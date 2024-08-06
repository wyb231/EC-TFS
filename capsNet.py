"""
License: Apache-2.0
Author: Huadong Liao
E-mail: naturomics.liao@gmail.com
"""

import tensorflow as tf

from config import cfg
from utils import get_batch_data
from utils import softmax
from utils import reduce_sum
from capsLayer import CapsLayer
from cbam import *


epsilon = 1e-9


class CapsNet(object):
    def __init__(self, is_training=True, height=28, width=28, channels=1, num_label=10):
        """
        Args:
            height: Integer, the height of inputs.
            width: Integer, the width of inputs.
            channels: Integer, the channels of inputs.
            num_label: Integer, the category number.
        """
        self.height = height
        self.width = width
        self.channels = channels
        self.num_label = num_label

        self.graph = tf.Graph()
        ############################### 注意力机制 ###############################
        # self.ca = ChannelAttention(channels)
        # self.sa = SpatialAttention()


        with self.graph.as_default():
            if is_training:
                self.X, self.labels = get_batch_data(cfg.dataset, cfg.batch_size, cfg.num_threads)

                self.Y = tf.one_hot(self.labels, depth=self.num_label, axis=1, dtype=tf.float32)

                self.build_arch()
                self.loss()
                self._summary()

                # t_vars = tf.trainable_variables()
                self.global_step = tf.Variable(0, name='global_step', trainable=False)
                self.optimizer = tf.train.AdamOptimizer()
                self.train_op = self.optimizer.minimize(self.total_loss, global_step=self.global_step)
            else:
                self.X = tf.placeholder(tf.float32, shape=(cfg.batch_size, self.height, self.width, self.channels))
                self.labels = tf.placeholder(tf.int32, shape=(cfg.batch_size, ))
                self.Y = tf.reshape(self.labels, shape=(cfg.batch_size, self.num_label, 1))
                self.build_arch()

        tf.logging.info('Seting up the main structure')

    def build_arch(self):
        with tf.variable_scope('Conv1_layer'):
            # Conv1, return tensor with shape [batch_size, 20, 20, 256]
            print("capsNet.py65卷积层input:::", self.X, self.height, self.width)

            conv1 = tf.contrib.layers.conv2d(self.X, num_outputs=256,
                                             kernel_size=9, stride=1,
                                             weights_regularizer=tf.contrib.layers.l2_regularizer(0.01),
                                             weights_initializer=tf.truncated_normal_initializer(stddev=0.01),
                                             padding='VALID')
            #conv1 = tf.nn.leaky_relu(conv1)

            print(conv1)

            ############################### 注意力机制 ###############################

            # ca = ChannelAttention(256)
            # sa = SpatialAttention()
            # conv1 = ca(conv1)
            # print("*************capsNet78.conv1***************", conv1.shape)
            # conv1 = sa(conv1)
            # print("*************capsNet80.conv1***************",conv1.shape)



        # Primary Capsules layer, return tensor with shape [batch_size, 1152, 8, 1]
        with tf.variable_scope('PrimaryCaps_layer'):
            primaryCaps = CapsLayer(num_outputs=32, vec_len=8, channels=self.channels, with_routing=False, layer_type='CONV')
            print("*************capsNet87.primaryCaps***************", primaryCaps)
            caps1 = primaryCaps(conv1, kernel_size=9, stride=2)
            print("*************capsNet89.caps1***************", caps1)

        # DigitCaps layer, return shape [batch_size, 10, 16, 1]
        with tf.variable_scope('DigitCaps_layer'):
            digitCaps = CapsLayer(num_outputs=self.num_label, vec_len=16, channels=self.channels, with_routing=True, layer_type='FC')
            self.caps2 = digitCaps(caps1)

        # Decoder structure in Fig. 2
        # 1. Do masking, how:
        with tf.variable_scope('Masking'):
            # a). calc ||v_c||, then do softmax(||v_c||)
            # [batch_size, 10, 16, 1] => [batch_size, 10, 1, 1]
            self.v_length = tf.sqrt(reduce_sum(tf.square(self.caps2),
                                               axis=2, keepdims=True) + epsilon)
            self.softmax_v = softmax(self.v_length, axis=1)
            # assert self.softmax_v.get_shape() == [cfg.batch_size, self.num_label, 1, 1]

            # b). pick out the index of max softmax val of the 10 caps
            # [batch_size, 10, 1, 1] => [batch_size] (index)
            self.argmax_idx = tf.to_int32(tf.argmax(self.softmax_v, axis=1))
            # assert self.argmax_idx.get_shape() == [cfg.batch_size, 1, 1]
            self.argmax_idx = tf.reshape(self.argmax_idx, shape=(cfg.batch_size, ))

            # Method 1.
            if not cfg.mask_with_y:
                # c). indexing
                # It's not easy to understand the indexing process with argmax_idx
                # as we are 3-dim animal
                masked_v = []
                for batch_size in range(cfg.batch_size):
                    v = self.caps2[batch_size][self.argmax_idx[batch_size], :]
                    masked_v.append(tf.reshape(v, shape=(1, 1, 16, 1)))

                self.masked_v = tf.concat(masked_v, axis=0)
                assert self.masked_v.get_shape() == [cfg.batch_size, 1, 16, 1]
            # Method 2. masking with true label, default mode
            else:
                self.masked_v = tf.multiply(tf.squeeze(self.caps2), tf.reshape(self.Y, (-1, self.num_label, 1)))
                self.v_length = tf.sqrt(reduce_sum(tf.square(self.caps2), axis=2, keepdims=True) + epsilon)

        # 2. Reconstructe the MNIST images with 3 FC layers
        # [batch_size, 1, 16, 1] => [batch_size, 16] => [batch_size, 512]
        with tf.variable_scope('Decoder'):
            vector_j = tf.reshape(self.masked_v, shape=(cfg.batch_size, -1))
            fc1 = tf.contrib.layers.fully_connected(vector_j, num_outputs=512)
            fc2 = tf.contrib.layers.fully_connected(fc1, num_outputs=1024)
            self.decoded = tf.contrib.layers.fully_connected(fc2,
                                                             num_outputs=self.height * self.width * self.channels,
                                                             activation_fn=tf.sigmoid)

    def loss(self):
        # 1. The margin loss

        # [batch_size, 10, 1, 1]
        # max_l = max(0, m_plus-||v_c||)^2
        max_l = tf.square(tf.maximum(0., cfg.m_plus - self.v_length))
        # max_r = max(0, ||v_c||-m_minus)^2
        max_r = tf.square(tf.maximum(0., self.v_length - cfg.m_minus))
        assert max_l.get_shape() == [cfg.batch_size, self.num_label, 1, 1]

        # reshape: [batch_size, 10, 1, 1] => [batch_size, 10]
        max_l = tf.reshape(max_l, shape=(cfg.batch_size, -1))
        max_r = tf.reshape(max_r, shape=(cfg.batch_size, -1))

        # calc T_c: [batch_size, 10]
        # T_c = Y, is my understanding correct? Try it.
        T_c = self.Y
        # [batch_size, 10], element-wise multiply
        L_c = T_c * max_l + cfg.lambda_val * (1 - T_c) * max_r

        self.margin_loss = tf.reduce_mean(tf.reduce_sum(L_c, axis=1))

        # 2. The reconstruction loss
        orgin = tf.reshape(self.X, shape=(cfg.batch_size, -1))
        squared = tf.square(self.decoded - orgin)
        self.reconstruction_err = tf.reduce_mean(squared)
        print("159self.caps2::", self.caps2)
        print("vvvvv_l::", self.v_length)
        print("vvvvv_l::", tf.squeeze(self.v_length))
        print("y::", self.Y)
        print("softmax::", self.softmax_v)
        print("argmax_idx::", self.argmax_idx)

        # 3. Total loss
        # The paper uses sum of squared error as reconstruction error, but we
        # have used reduce_mean in `# 2 The reconstruction loss` to calculate
        # mean squared error. In order to keep in line with the paper,the
        # regularization scale should be 0.0005*784=0.392
        self.total_loss = self.margin_loss + cfg.regularization_scale * self.reconstruction_err


    # Summary
    def _summary(self):
        correct_prediction = tf.equal(tf.to_int32(self.labels), self.argmax_idx)
        self.accuracy = tf.reduce_sum(tf.cast(correct_prediction, tf.float32))

        print("180summary_test:::", self.accuracy, self.accuracy.shape)
        train_summary = []
        train_summary.append(tf.summary.scalar('val/accuracy', self.accuracy))
        train_summary.append(tf.summary.scalar('train/margin_loss', self.margin_loss))
        train_summary.append(tf.summary.scalar('train/reconstruction_loss', self.reconstruction_err))
        train_summary.append(tf.summary.scalar('train/total_loss', self.total_loss))
        recon_img = tf.reshape(self.decoded, shape=(cfg.batch_size, self.height, self.width, self.channels))
        self.recon_img = recon_img
        train_summary.append(tf.summary.image('reconstruction_img', recon_img))
        self.train_summary = tf.summary.merge(train_summary)

        self.test_loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(labels=self.labels, logits=tf.squeeze(self.v_length)))
