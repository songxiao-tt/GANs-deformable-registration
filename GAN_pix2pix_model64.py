from __future__ import print_function, division

from keras.callbacks import TensorBoard
import keras.backend as K
import tensorflow as tf

from keras.layers import BatchNormalization
from keras.layers import Input, Dropout, Concatenate, Cropping3D
from keras.layers.advanced_activations import LeakyReLU
from keras.layers.convolutional import UpSampling3D, Conv3D

from keras.optimizers import Adam
from keras.models import Model

import numpy as np
import datetime
import nrrd
import os

from ImageRegistrationGANs.data_loader import DataLoader

__author__ = 'elmalakis'


class GAN_pix2pix():

    def __init__(self):

        K.set_image_data_format('channels_last')  # set format
        self.DEBUG = 1

        # Input shape
        self.img_rows = 128
        self.img_cols = 128
        self.img_vols = 128
        self.channels = 1
        self.img_shape = (self.img_rows, self.img_cols, self.img_vols, self.channels )


        self.batch_sz = 1 # for testing locally to avoid memory allocation
        self.data_loader = DataLoader(batch_sz=self.batch_sz, dataset_name='fly', crop_size=(self.img_rows, self.img_cols, self.img_vols))


        # Calculate output shape of D (PatchGAN)
        patch = int(self.img_rows / 2 ** 4)
        self.disc_patch = (patch, patch, patch, 1)

        # Number of filters in the first layer of G and D
        self.gf = 64
        self.df = 64

        optimizer = Adam(0.0002, 0.5)

        # Build and compile the discriminator
        self.discriminator = self.build_discriminator()
        self.discriminator.compile(loss='mse',
                                   optimizer=optimizer,
                                   metrics=['accuracy'])

        # -------------------------
        # Construct Computational
        #   Graph of Generator
        # -------------------------

        # Build the generator
        self.generator = self.build_generator()

        # Input images and their conditioning images
        img_A = Input(shape=self.img_shape)
        img_B = Input(shape=self.img_shape)

        # By conditioning on B generate a fake version of A
        fake_A = self.generator(img_B)

        # For the combined model we will only train the generator
        self.discriminator.trainable = False

        # Discriminators determines validity of translated images / condition pairs
        valid = self.discriminator([fake_A, img_B])

        self.combined = Model(inputs=[img_A, img_B], outputs=[valid, fake_A])
        self.combined.compile(loss=['mse', 'mae'],
                              loss_weights=[1, 100],
                              optimizer=optimizer)

        if self.DEBUG:
            log_path = '/nrs/scicompsoft/elmalakis/GAN_Registration_Data/flydata/forSalma/lo_res/logs_ganpix2pix/'
            self.callback = TensorBoard(log_path)
            self.callback.set_model(self.combined)


    def build_generator(self):
        """U-Net Generator"""

        def conv2d(layer_input, filters, f_size=4, bn=True):
            """Layers used during downsampling"""
            d = Conv3D(filters, kernel_size=f_size, strides=2, padding='same')(layer_input)
            d = LeakyReLU(alpha=0.2)(d)
            if bn:
                d = BatchNormalization(momentum=0.8)(d)
            return d

        def deconv2d(layer_input, skip_input, filters, f_size=4, dropout_rate=0.5): # dropout is 50 ->change from the implementaion
            """Layers used during upsampling"""
            u = UpSampling3D(size=2)(layer_input)
            u = Conv3D(filters, kernel_size=f_size, padding='same', activation='relu')(u) # remove the strides
            if dropout_rate:
                u = Dropout(dropout_rate)(u)
            u = BatchNormalization(momentum=0.8)(u)
            u = Concatenate()([u, skip_input])
            return u

        # Image input
        d0 = Input(shape=self.img_shape)    #128x128x182

        # Downsampling
        d1 = conv2d(d0, self.gf, bn=False)   #64x64x64
        d2 = conv2d(d1, self.gf*2)           #32x32x32
        d3 = conv2d(d2, self.gf*4)           #16x16x16
        d4 = conv2d(d3, self.gf*8)           #8x8x8
        d5 = conv2d(d4, self.gf*8)           #4x4x4
        d6 = conv2d(d5, self.gf*8)           #2x2x2
        d7 = conv2d(d6, self.gf*8)           #1x1x1

        # Upsampling
        u1 = deconv2d(d7, d6, self.gf*8)
        u2 = deconv2d(d6, d5, self.gf*8)
        u3 = deconv2d(u2, d4, self.gf*8)
        u4 = deconv2d(u3, d3, self.gf*4)
        u5 = deconv2d(u4, d2, self.gf*2)
        u6 = deconv2d(u5, d1, self.gf)

        u7 = UpSampling3D(size=2)(u6)
        output_img = Conv3D(self.channels, kernel_size=4, strides=1, padding='same', activation='tanh')(u7)

        return Model(d0, output_img)


    def build_discriminator(self):

        def d_layer(layer_input, filters, f_size=4, bn=True):
            """Discriminator layer"""
            d = Conv3D(filters, kernel_size=f_size, strides=2, padding='same')(layer_input)
            d = LeakyReLU(alpha=0.2)(d)
            if bn:
                d = BatchNormalization(momentum=0.8)(d)
            return d

        img_A = Input(shape=self.img_shape)
        img_B = Input(shape=self.img_shape)

        # Concatenate image and conditioning image by channels to produce input
        combined_imgs = Concatenate(axis=-1)([img_A, img_B])

        d1 = d_layer(combined_imgs, self.df, bn=False)
        d2 = d_layer(d1, self.df*2)
        d3 = d_layer(d2, self.df*4)
        d4 = d_layer(d3, self.df*8)

        validity = Conv3D(1, kernel_size=4, strides=1, padding='same')(d4)

        return Model([img_A, img_B], validity)


    """
    Training
    """
    def train(self, epochs, batch_size=1, sample_interval=50):
        DEBUG =1
        path = '/nrs/scicompsoft/elmalakis/GAN_Registration_Data/flydata/forSalma/lo_res/'
        os.makedirs(path+'generated/' , exist_ok=True)

        # Adversarial loss ground truths
        valid = np.ones((batch_size,) + self.disc_patch)
        fake = np.zeros((batch_size,) + self.disc_patch)

        start_time = datetime.datetime.now()
        for epoch in range(epochs):
            for batch_i, (batch_img, batch_img_template) in enumerate(self.data_loader.load_batch()):
                # ---------------------
                #  Train Discriminator
                # ---------------------
                # Condition on B and generate a translate
                fake_A = self.generator.predict(batch_img)
                self.discriminator.trainable = True
                # Train the discriminators (original images = real / generated = Fake)
                d_loss_real = self.discriminator.train_on_batch([batch_img, batch_img_template], valid)
                d_loss_fake = self.discriminator.train_on_batch([fake_A, batch_img_template], fake)
                d_loss = 0.5 * np.add(d_loss_real, d_loss_fake)

                # -----------------
                #  Train Generator
                # -----------------

                # Train the generators
                self.discriminator.trainable = False
                g_loss = self.combined.train_on_batch([batch_img, batch_img_template], [valid, batch_img]) # The original implemntation has batch_img in the output

                elapsed_time = datetime.datetime.now() - start_time
                # Plot the progress
                print ("[Epoch %d/%d] [Batch %d/%d] [D loss: %f, acc: %3d%%] [G loss: %f] time: %s" % (epoch, epochs,
                                                                        batch_i, self.data_loader.n_batches,
                                                                        d_loss[0], 100*d_loss[1],
                                                                        g_loss[0],
                                                                        elapsed_time))


                if self.DEBUG:
                    self.write_log(self.callback, ['g_loss'], [g_loss[0]], batch_i)
                    self.write_log(self.callback, ['d_loss'], [d_loss[0]], batch_i)

                # If at save interval => save generated image samples
                if batch_i % sample_interval == 0:
                    self.sample_images(epoch, batch_i)


    def write_log(self, callback, names, logs, batch_no):
        #https://github.com/eriklindernoren/Keras-GAN/issues/52
        for name, value in zip(names, logs):
            summary = tf.Summary()
            summary_value = summary.value.add()
            summary_value.simple_value = value
            summary_value.tag = name
            callback.writer.add_summary(summary, batch_no)
            callback.writer.flush()


    def sample_images(self, epoch, batch_i):
        path = '/nrs/scicompsoft/elmalakis/GAN_Registration_Data/flydata/forSalma/lo_res/'
        os.makedirs(path+'generated/' , exist_ok=True)

        idx, imgs_S, imgs_S_mask = self.data_loader.load_data(is_validation=True)
        imgs_T = self.data_loader.img_template


        predict_img = np.zeros(imgs_S.shape, dtype=imgs_S.dtype)

        input_sz = (64, 64, 64)
        step = (32, 32, 32)

        gap = (int((input_sz[0] - step[0]) / 2), int((input_sz[1] - step[1]) / 2), int((input_sz[2] - step[2]) / 2))
        start_time = datetime.datetime.now()



        for row in range(0, imgs_S.shape[0] - input_sz[0], step[0]):
            for col in range(0, imgs_S.shape[1] - input_sz[1], step[1]):
                for vol in range(0, imgs_S.shape[2] - input_sz[2], step[2]):

                    # patch_sub_img = np.zeros((1, input_sz[0], input_sz[1], input_sz[2], 1), dtype=imgs_S.dtype)
                    # patch_templ_img = np.zeros((1, input_sz[0], input_sz[1], input_sz[2], 1), dtype=imgs_T.dtype)
                    #
                    # patch_sub_img[0, :, :, :, 0] = imgs_S[row:row + input_sz[0],
                    #                                     col:col + input_sz[1],
                    #                                     vol:vol + input_sz[2]]
                    # patch_templ_img[0, :, :, :, 0] = imgs_T[row:row + input_sz[0],
                    #                                  col:col + input_sz[1],
                    #                                  vol:vol + input_sz[2]]

                    patch_predict_warped = self.generator.predict(imgs_S)

                    predict_img[row + gap[0]:row + gap[0] + step[0],
                                col + gap[1]:col + gap[1] + step[1],
                                vol + gap[2]:vol + gap[2] + step[2]] = patch_predict_warped[0, :, :, :, 0]

        elapsed_time = datetime.datetime.now() - start_time
        print(" --- Prediction time: %s" % (elapsed_time))

        nrrd.write(path+"generated/%d_%d_%d" % (epoch, batch_i, idx), predict_img)

        file_name = 'gan_network'

        # save the whole network
        gan.combined.save(file_name + '.whole.h5', overwrite=True)
        print('Save the whole network to disk as a .whole.h5 file')
        model_jason = gan.combined.to_json()
        with open(file_name + '_arch.json', 'w') as json_file:
            json_file.write(model_jason)
        gan.combined.save_weights(file_name + '_weights.h5', overwrite=True)
        print('Save the network architecture in .json file and weights in .h5 file')

        # save the encoder network
        gan.generator.save(file_name + '.gen.h5', overwrite=True)
        print('Save the generator network to disk as a .whole.h5 file')
        model_jason = gan.combined.to_json()
        with open(file_name + '_gen_arch.json', 'w') as json_file:
            json_file.write(model_jason)
        gan.combined.save_weights(file_name + '_gen_weights.h5', overwrite=True)
        print('Save the generator architecture in .json file and weights in .h5 file')


if __name__ == '__main__':
    # Use GPU
    K.tensorflow_backend._get_available_gpus()
    # launch tf debugger
    #sess = K.get_session()
    #sess = tf_debug.LocalCLIDebugWrapperSession(sess)
    #K.set_session(sess)

    gan = GAN_pix2pix()
    gan.train(epochs=20000, batch_size=1, sample_interval=200)





