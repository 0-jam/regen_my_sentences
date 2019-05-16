# OLD VERSION of the text generator model
import functools
import json
import time
from pathlib import Path
from random import choice

import numpy as np
import tensorflow as tf
from tensorflow import keras
from tqdm import tqdm

from modules.wakachi.mecab import divide_word

config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=True)
config.gpu_options.allow_growth = True
config.gpu_options.per_process_gpu_memory_fraction = 0.2
tf.enable_eager_execution(config=config)

SEQ_LENGTH = 100
BUFFER_SIZE = 10000
NUM_HIDDEN_LAYERS = 1


# Character-based model
class TextModel(object):
    tokenizer = keras.preprocessing.text.Tokenizer(filters='\\\t\n', oov_token='<oov>', char_level=True)

    def __init__(self, embedding_dim, units, batch_size, text, cpu_mode=False):
        # Hyper parameters
        self.embedding_dim = embedding_dim
        self.units = units
        self.batch_size = batch_size
        self.cpu_mode = not tf.test.is_gpu_available() or cpu_mode

        # Vectorize the text
        self.tokenizer.fit_on_texts(text)
        self.vocab2idx = self.tokenizer.word_index
        # Index 0 is preserved in the Keras tokenizer for the unknown word, but it's not included in vocab2idx
        self.idx2vocab = dict([(i, v) for v, i in self.vocab2idx.items()])
        self.idx2vocab[0] = '<oov>'
        self.vocab_size = len(self.vocab2idx) + 1
        text_size = len(text)
        print("Text has {} characters ({} unique characters)".format(text_size, self.vocab_size - 1))

        # Creating a mapping from unique characters to indices
        text_as_int = self.vocab_to_indices(text)

        # The maximum length sentence we want for single input in characters
        chunks = tf.data.Dataset.from_tensor_slices(text_as_int).batch(SEQ_LENGTH + 1, drop_remainder=True)
        self.dataset = chunks.map(self.split_into_target)
        self.dataset = self.dataset.shuffle(BUFFER_SIZE).batch(self.batch_size, drop_remainder=True)
        self.steps_per_epoch = text_size // SEQ_LENGTH // batch_size

        self.model = self.build_model()
        self.model.summary()

    def build_model(self):
        # Disable CuDNN if GPU is not available
        if self.cpu_mode:
            gru = functools.partial(
                keras.layers.GRU,
                recurrent_activation='sigmoid',
            )
        else:
            gru = keras.layers.CuDNNGRU

        grus = []
        for _ in range(NUM_HIDDEN_LAYERS):
            grus.append(gru(
                self.units,
                return_sequences=True,
                stateful=True,
                recurrent_initializer='glorot_uniform'
            ))
            grus.append(keras.layers.Dropout(0.5))

        return keras.Sequential(
            [keras.layers.Embedding(self.vocab_size, self.embedding_dim, batch_input_shape=[self.batch_size, None])]
            + grus
            + [keras.layers.Dense(self.vocab_size)]
        )

    @staticmethod
    def loss(labels, logits):
        return tf.keras.losses.sparse_categorical_crossentropy(labels, logits, from_logits=True)

    @staticmethod
    def callbacks(model_dir):
        return [
            keras.callbacks.ModelCheckpoint(str(model_dir.joinpath("ckpt_{epoch}")), save_weights_only=True, period=5, verbose=1),
            keras.callbacks.EarlyStopping(monitor='loss', patience=3, verbose=1)
        ]

    def compile(self):
        self.model.compile(optimizer=tf.train.AdamOptimizer(), loss=self.loss)

    def fit(self, model_dir, epochs):
        start_time = time.time()
        history = self.model.fit(self.dataset.repeat(), epochs=epochs, steps_per_epoch=self.steps_per_epoch, callbacks=self.callbacks(model_dir))
        elapsed_time = time.time() - start_time
        print("Time taken for learning {} epochs: {:.3f} minutes ({:.3f} minutes / epoch )".format(epochs, elapsed_time / 60, (elapsed_time / epochs) / 60))

        return history

    # Train model from the dataset
    # TODO: Early stopping during the training
    def train(self):
        optimizer = tf.train.AdamOptimizer()
        loss_f = tf.losses.sparse_softmax_cross_entropy

        start = time.time()
        for (batch, (input, target)) in enumerate(self.dataset):
            with tf.GradientTape() as tape:
                # feeding the hidden state back into the model
                predictions = self.model(input)

                # reshape target to make loss function expect the target
                loss = loss_f(target, predictions)

            gradients = tape.gradient(loss, self.model.variables)
            optimizer.apply_gradients(zip(gradients, self.model.variables))

            print("Batch: {}, Loss: {:.4f}".format(batch + 1, loss), end="\r")

        elapsed_time = time.time() - start
        print("Time taken for this epoch: {:.3f} sec, Loss: {:.3f}".format(
            elapsed_time,
            loss
        ))

        return loss.numpy()

    def save(self, model_dir):
        if Path.is_dir(model_dir) is not True:
            Path.mkdir(model_dir, parents=True)

        with model_dir.joinpath('parameters.json').open('w', encoding='utf-8') as params:
            params.write(json.dumps(self.parameters()))

        self.model.save_weights(str(Path(model_dir.joinpath('weights'))))

    def load(self, model_dir):
        self.model.load_weights(self.path(Path(model_dir)))

    def generate_text(self, start_string=None, gen_size=1, temp=1.0, delimiter=None):
        if not start_string:
            start_string = choice(self.idx2vocab)

        generated_text = [start_string]
        # Vectorize start string
        try:
            input_eval = tf.expand_dims(self.vocab_to_indices(start_string), 0)
            print('Start string:', start_string)
        except KeyError:
            print('Unknown word included')
            return ''

        # Randomness of text generation
        temperature = temp

        count = 0
        self.model.reset_states()
        with tqdm(desc='Generating...', total=gen_size) as pbar:
            while count < gen_size:
                predictions = self.model(input_eval)
                # remove the batch dimension
                predictions = tf.squeeze(predictions, 0)

                # Using the multinomial distribution to predict the word returned by the model
                predictions = predictions / temperature
                predicted_id = tf.random.categorical(predictions, num_samples=1)[-1, 0].numpy()

                # Pass the predicted word as the next input to the model along with the previous hidden state
                input_eval = tf.expand_dims([predicted_id], 0)

                try:
                    char = self.idx2vocab[predicted_id]
                except KeyError:
                    # Mark as unknown word if predicted ID is out of bounds
                    char = '<oob>'
                generated_text.append(char)

                if char == delimiter or not delimiter:
                    count += 1
                    pbar.update()

        return generated_text

    # Return the path to <ckpt_dir>/checkpoint
    @staticmethod
    def path(ckpt_dir):
        return tf.train.latest_checkpoint(str(Path(ckpt_dir)))

    # Return model settings as dict
    def parameters(self):
        return {
            'embedding_dim': self.embedding_dim,
            'units': self.units,
            'batch_size': self.batch_size,
            'cpu_mode': self.cpu_mode
        }

    # Create input and target texts from the text
    @staticmethod
    def split_into_target(chunk):
        input_text = chunk[:-1]
        target_text = chunk[1:]

        return input_text, target_text

    # Convert string to numbers
    def vocab_to_indices(self, sentence):
        return np.array(self.tokenizer.texts_to_sequences(sentence.lower())).reshape(-1,)


# Word-based model
# Convert text into one-hot vector
class WordModel(TextModel):
    tokenizer = keras.preprocessing.text.Tokenizer(filters='\\\t\n', oov_token='<oov>', char_level=False, num_words=15000)

    def __init__(self, embedding_dim, units, batch_size, text, cpu_mode=True):
        words = text.split()
        super().__init__(embedding_dim, units, batch_size, words, cpu_mode)

    # Convert word to numbers
    # Automatically convert string into words
    def vocab_to_indices(self, sentence):
        if type(sentence) == str:
            sentence = divide_word(sentence.lower())

        return np.array(self.tokenizer.texts_to_sequences([sentence])).reshape(-1,)
