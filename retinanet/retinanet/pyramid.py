from typing import Tuple, List

import keras
from keras.layers import Conv2D, Add, Activation, Concatenate
from tensorflow import Tensor

from retinanet.retinanet.layers import UpSample


def graph_pyramid_features(c2: Tensor, c3: Tensor, c4: Tensor, c5: Tensor,
                           feature_size: int = 256,
                           use_p2: bool = False) -> List[Tensor]:
    # Reduce the feature maps by operating 1 x 1 convolution
    # UpSample p5 to the same as the shape of the c4
    p5 = Conv2D(feature_size, kernel_size=1, strides=1, padding='same', name='c5_reduced')(c5)
    p5_upsampled = UpSample(name='p5_upsampled')([p5, c4])
    p5_output = Conv2D(feature_size, kernel_size=3, strides=1, padding='same', name='p5')(p5)

    # Add p5 element-wise to c4
    p4 = Conv2D(feature_size, kernel_size=1, strides=1, padding='same', name='c4_reduced')(c4)
    p4 = Add(name='p4_added')([p5_upsampled, p4])
    p4_upsampled = UpSample(name='p4_upsampled')([p4, c3])
    p4_output = keras.layers.Conv2D(feature_size, kernel_size=3, strides=1, padding='same', name='p4')(p4)

    # Add p4 elementwise to c3
    p3 = Conv2D(feature_size, kernel_size=1, strides=1, padding='same', name='c3_reduced')(c3)
    p3 = Add(name='p3_added')([p4_upsampled, p3])
    p3_output = Conv2D(feature_size, kernel_size=3, strides=1, padding='same', name='p3')(p3)

    # Add p3 elementwise to c2
    p2_output = None
    if use_p2:
        p3_upsampled = UpSample(name='p3_upsampled')([p3, c2])
        p2 = Conv2D(feature_size, kernel_size=1, strides=1, padding='same', name='c2_reduced')(c2)
        p2 = Add(name='p2_added')([p3_upsampled, p2])
        p2_output = Conv2D(feature_size, kernel_size=3, strides=1, padding='same', name='p2')(p2)

    # "p6 is obtained via a 3x3 stride-2 conv on c5"
    # Paper: Here we introduce P6 only for covering a larger anchor scale of 512.
    #        P6 is simply a stride two subsampling of P5
    p6_output = Conv2D(feature_size, kernel_size=3, strides=2, padding='same', name='p6')(c5)
    p7 = Activation('relu', name='c6_relu')(p6_output)

    # p6 -> relu -> 3 x 3 convolution with stride 2 -> p7
    p7_output = Conv2D(feature_size, kernel_size=3, strides=2, padding='same', name='p7')(p7)

    response = list()
    if use_p2:
        response.append(p2_output)
    response.extend([p3_output, p4_output, p5_output, p6_output, p7_output])
    return response


def apply_pyramid_features(features, clf_subnet, reg_subnet) -> Tuple[Tensor, Tensor]:
    """
    :param features: a list of pyramid features
    :param clf_subnet: classification sub network
    :param reg_subnet: regression sub network
    :return pyramid features -> Classification Subnet
                             -> Regression Subnet
    """
    clf = Concatenate(axis=1, name='clf')([clf_subnet(f) for f in features])
    reg = Concatenate(axis=1, name='reg')([reg_subnet(f) for f in features])
    return clf, reg
