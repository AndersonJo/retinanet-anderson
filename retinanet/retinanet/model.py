from typing import List, Tuple

import keras
import tensorflow as tf
from keras import Model
from keras.layers import Input, Concatenate

from retinanet.anchor.information import AnchorInfo
from retinanet.backbone import load_backbone
from retinanet.retinanet import losses
from retinanet.retinanet.initializer import PriorProbability
from retinanet.retinanet.layers import RegressBoxes, ClipBoxes, Anchor
from retinanet.retinanet.pyramid import graph_pyramid_features, apply_pyramid_features
from retinanet.utils.filter_detections import FilterDetections


class RetinaNet(object):
    def __init__(self,
                 backbone: str,
                 n_class: int,
                 anchor_info: AnchorInfo = AnchorInfo(),
                 fpn_feature_size: int = 256):
        """
        # Basic Parameters
        :param n_class: the number of classes
        :param n_anchor: the number of anchors
        :param anchor_info: AnchorInfo

        # Backbone Parameters
        :param backbone: The name of the backbone Model

        :param freeze: freeze the weights of the backbone model
        :param weights: weights file path. if None, it uses pre-trained ImageNet weights.

        # Pyramid Parameters
        :param fpn_feature_size: the feature size of the pyramid network
        """
        # Initialize basic parameters
        self.n_class = n_class
        self.n_anchor = anchor_info.count_anchors()
        self.anchor_info = anchor_info

        # Initialize Feature Pyramid Network
        self.fpn_feature_size = fpn_feature_size

        # Initialize Backbone Network
        self.inputs = Input(shape=(None, None, 3), name='input')
        self.backbone = load_backbone(backbone)

        # Initialize Models
        self._model_train = None
        self._model_pred = None

    @property
    def pred_model(self) -> Model:
        return self._model_pred

    @property
    def train_model(self) -> Model:
        return self._model_train

    def create_retinanet(self,
                         # Backbone
                         freeze_backbone: bool = False,
                         weights: str = None,
                         pyramids: List[str] = ('P2', 'P3', 'P4', 'P5', 'P6', 'P7'),

                         # Sub Networks
                         clf_feature_size: int = 256,
                         reg_feature_size: int = 256,
                         prior_probability=0.01,

                         # NMS
                         use_nms=True,

                         # Debug
                         debug=True
                         ) -> Tuple[Model, Model]:

        # Initialize Backbone Model
        backbone_model = self.backbone.create_backbone_model(self.inputs, freeze=freeze_backbone)

        # Create Sub Networks
        clf_subnet = self.create_classification_subnet(clf_feature_size=clf_feature_size,
                                                       prior_prob=prior_probability)

        reg_subnet = self.create_regression_subnet(reg_feature_size=reg_feature_size)

        # Apply Feature Pyramid
        pyramid_features = graph_pyramid_features(*backbone_model.outputs)
        subnets = apply_pyramid_features(pyramid_features, clf_subnet, reg_subnet)

        # Create RetinaNet Model
        self._model_train = Model(inputs=self.inputs, outputs=subnets, name='retinanet')

        # Load weights
        if weights is None:
            weights = self.backbone.download_imagenet()
        self._model_train.load_weights(weights, by_name=True, skip_mismatch=False)

        self._model_train.compile(
            loss={'regression': losses.smooth_l1_loss(),
                  'classification': losses.focal_loss()},
            optimizer=keras.optimizers.adam(lr=1e-5, clipnorm=0.001))

        self._create_prediction_model(pyramids=pyramids, use_nms=use_nms)

        return self.train_model, self.pred_model

    def _create_prediction_model(self, pyramids: List[str] = ('P2', 'P3', 'P4', 'P5', 'P6', 'P7'),
                                 use_nms=True,
                                 name='retinanet-prediction', ):
        # Get Pyramid Features
        pyramids = list(map(lambda p: p.lower(), pyramids))
        pyramid_features = [self.train_model.get_layer(p_name).output for p_name in pyramids]

        anchors = self.generate_anchors(pyramid_features)

        clf_output = self.train_model.outputs[0]  # (1, points 360360, n_labels)
        reg_output = self.train_model.outputs[1]  # (1, points 360360, 4)

        boxes = RegressBoxes(name='boxes')([anchors, reg_output])
        boxes = ClipBoxes(name='clipped_boxes')([self.inputs, boxes])

        # Apply NMS / Score threshold / Select top-k
        outputs = FilterDetections(nms=use_nms, parallel_iterations=128, name='nms_filter')([boxes, clf_output])

        # construct the model
        model = keras.models.Model(inputs=self.train_model.inputs, outputs=outputs, name=name)
        self._model_pred = model

        return self.pred_model

    def generate_anchors(self, pyramid_features: List[tf.Tensor]):
        anchor_info = self.anchor_info

        anchors = list()
        for i, feature in enumerate(pyramid_features):
            anchor_layer = Anchor(size=anchor_info.sizes[i],
                                  stride=anchor_info.strides[i],
                                  ratios=anchor_info.ratios,
                                  scales=anchor_info.scales,
                                  )(feature)
            anchors.append(anchor_layer)

        return Concatenate(axis=1, name='anchors')(anchors)

    def create_regression_subnet(self, reg_feature_size: int = 256) -> Model:
        """
        :param reg_feature_size: Regression subnet's feature size

        Every layers are the same as the ones in classification sub network except the final one.
        All layers in subnets are initialized with bias b = 0 and a Guassian weight fill with stddev = 0.01
        :return: regression subnet model
        """
        n_anchor = self.n_anchor
        fpn_feature_size = self.fpn_feature_size

        def regr_conv1(h, name):
            return keras.layers.Conv2D(
                filters=reg_feature_size,
                kernel_size=3,
                strides=1,
                padding='same',
                activation='relu',
                kernel_initializer=keras.initializers.normal(mean=0.0, stddev=0.01, seed=None),
                bias_initializer='zeros',
                name=name,
            )(h)

        def regr_conv2(h, name):
            return keras.layers.Conv2D(
                filters=n_anchor * 4,
                kernel_size=3,
                strides=1,
                padding='same',
                kernel_initializer=keras.initializers.normal(mean=0.0, stddev=0.01, seed=None),
                bias_initializer='zeros',
                name=name,
            )(h)

        inputs = Input(shape=(None, None, fpn_feature_size), name='reg_subnet_input')
        h = regr_conv1(inputs, 'reg_subnet_1')
        h = regr_conv1(h, 'reg_subnet_2')
        h = regr_conv1(h, 'reg_subnet_3')
        h = regr_conv1(h, 'reg_subnet_4')
        h = regr_conv2(h, 'reg_subnet_5')

        h = keras.layers.Reshape((-1, 4), name='reg_subnet_reshape')(h)
        h = keras.layers.Activation('linear', name='reg_subnet_linear_activation')(h)
        return keras.models.Model(inputs=inputs, outputs=h, name='reg_subnet_model')

    def create_classification_subnet(self,
                                     clf_feature_size: int = 256,
                                     prior_prob: float = 0.01) -> Model:
        """
        :param clf_feature_size: Classification subnet's feature size
        :param prior_prob:
        :param name:
        :return:
        """
        n_class = self.n_class
        n_anchor = self.n_anchor
        fpn_feature_size = self.fpn_feature_size

        def clf_conv1(h, name):
            return keras.layers.Conv2D(
                filters=clf_feature_size,
                activation='relu',
                name=name,
                kernel_initializer=keras.initializers.normal(mean=0.0, stddev=0.01, seed=None),
                bias_initializer='zeros',
                kernel_size=3,
                strides=1,
                padding='same'
            )(h)

        def clf_conv2(h, name):
            return keras.layers.Conv2D(
                filters=n_class * n_anchor,
                kernel_initializer=keras.initializers.zeros(),
                bias_initializer=PriorProbability(prior=prior_prob),
                name=name,
                kernel_size=3,
                strides=1,
                padding='same')(h)

        inputs = Input(shape=(None, None, fpn_feature_size), name='clf_subnet_input')

        h = clf_conv1(inputs, 'clf_subnet_1')
        h = clf_conv1(h, 'clf_subnet_2')
        h = clf_conv1(h, 'clf_subnet_3')
        h = clf_conv1(h, 'clf_subnet_4')
        h = clf_conv2(h, 'clf_subnet_5_with_prior_bias')

        h = keras.layers.Reshape((-1, n_class), name='clf_subnet_reshape')(h)
        h = keras.layers.Activation('sigmoid', name='clf_subnet_sigmoid')(h)
        return Model(inputs=inputs, outputs=h, name='clf_subnet_model')

    def load_checkpoint(self, checkpoint: str) -> Model:
        """
        :param checkpoint: Checkpoint file path. It resume training from the checkpoint.
        :return: RetinaNet Model
        """
        model = keras.models.load_model(checkpoint, custom_objects=self.backbone.custom_objects)
        return model
