import sys
import os
import jax
import tensorflow_datasets as tfds

import tensorflow as tf
import numpy as np
import jax.numpy as jnp
import optax
import wandb
import logging

from galsim_jax.dif_models import AutoencoderKLModule
from galsim_jax.utils import (
    save_checkpoint,
    load_checkpoint,
    get_wandb_local_dir,
    create_folder,
    save_plot_as_image,
    save_samples,
    get_git_commit_version,
    get_activation_fn,
    get_optimizer,
    norm_values_one_diff,
    new_optimizer,
)

from galsim_jax.convolution import convolve_kpsf

from galsim_jax.datasets import cosmos

from jax.lib import xla_bridge
from astropy.stats import mad_std
from tensorflow_probability.substrates import jax as tfp
from flax import linen as nn  # Linen API
from jax import random

from functools import partial

from tqdm.auto import tqdm
from absl import app
from absl import flags

# logging.getLogger("tfds").setLevel(logging.ERROR)

# flags.DEFINE_string("input_folder", "/data/tensorflow_datasets/", "Location of the input images")
flags.DEFINE_string("dataset", "Cosmos/25.2", "Suite of simulations to learn from")
# flags.DEFINE_string("output_dir", "./weights/gp-sn1v5", "Folder where to store model.")
flags.DEFINE_integer("batch_size", 16, "Size of the batch to train on.")
flags.DEFINE_float("learning_rate", 5e-2, "Learning rate for the optimizer.")
# flags.DEFINE_integer("training_steps", 125000, "Number of training steps to run.")
flags.DEFINE_integer("training_steps", 18000, "Number of training steps to run.")
# flags.DEFINE_string("train_split", "90%", "How much of the training set to use.")
# flags.DEFINE_boolean('prob_output', True, 'The encoder has or not a probabilistic output')
flags.DEFINE_float("reg_value", 1e-6, "Regularization value of the KL Divergence.")
flags.DEFINE_integer("gpu", 0, "Index of the GPU to use, e.g.: 0, 1, 2, etc.")
# flags.DEFINE_string(
#     "experiment", "model_1", "Type of experiment, e.g. 'model_1', 'model_2', etc."
# )
flags.DEFINE_string("project", "VAE-SD", "Name of the project, e.g.: 'VAE-SD'")
flags.DEFINE_string(
    "name", "test_Cosmos_Conv2", "Name for the experiment, e.g.: 'dim_64_kl_0.01'"
)
flags.DEFINE_string(
    "act_fn", "gelu", "Activation function, e.g.: 'gelu', 'leaky_relu', etc."
)
flags.DEFINE_string("opt", "adafactor", "Optimizer, e.g.: 'adam', 'adamw'")
flags.DEFINE_integer("resblocks", 2, "Number of resnet blocks.: 1, 2.")
flags.DEFINE_integer("step_sch", 50000, "Steps for the lr_schedule")
flags.DEFINE_string(
    "noise",
    "Pixel",
    "Type of noise, Fourier for correlated, Pixel white Gaussian noise",
)
flags.DEFINE_float(
    "alpha", 0.0001, "Coefficient of reduction of initial learning rate"
)

FLAGS = flags.FLAGS

# Loading distributions and bijectors from TensorFlow Probability (JAX version)
tfd = tfp.distributions
tfb = tfp.bijectors


def main(_):
    # Checking for GPU access
    print("Device: {}".format(xla_bridge.get_backend().platform))

    # Checking the GPU available
    gpus = jax.devices("gpu")
    print("Number of avaliable devices : {}".format(len(gpus)))

    # Ensure TF does not see GPU and grab all GPU memory.
    tf.config.set_visible_devices([], device_type="GPU")

    # Loading the dataset and transforming it to NumPy Arrays
    train_dset, info = tfds.load(name=FLAGS.dataset, with_info=True, split="train")

    # What's in our dataset:
    # info

    def input_fn(mode="train", batch_size=FLAGS.batch_size):
        """
        mode: 'train' or 'test'
        """

        def preprocess_image(data):
            # Reshape 'psf' and 'image' to (128, 128, 1)
            data["kpsf_real"] = tf.expand_dims(data["kpsf_real"], axis=-1)
            data["kpsf_imag"] = tf.expand_dims(data["kpsf_imag"], axis=-1)
            data["image"] = tf.expand_dims(data["image"], axis=-1)
            return data

        if mode == "train":
            dataset = tfds.load(FLAGS.dataset, split="train")
            dataset = dataset.repeat()
            dataset = dataset.shuffle(10000)
        else:
            dataset = tfds.load(FLAGS.dataset, split="test")

        dataset = dataset.batch(batch_size, drop_remainder=True)
        dataset = dataset.map(preprocess_image)  # Apply data preprocessing
        dataset = dataset.prefetch(
            -1
        )  # fetch next batches while training current one (-1 for autotune)
        return dataset

    # Dataset as a numpy iterator
    dset = input_fn().as_numpy_iterator()

    # Generating a random key for JAX
    rng, rng_2 = jax.random.PRNGKey(0), jax.random.PRNGKey(1)
    # Size of the input to initialize the encoder parameters
    batch_autoenc = jnp.ones((1, 128, 128, 1))

    latent_dim = 128
    act_fn = get_activation_fn(FLAGS.act_fn)

    # Initializing the AutoEncoder
    Autoencoder = AutoencoderKLModule(
        ch_mult=(1, 2, 4, 8, 16),
        num_res_blocks=FLAGS.resblocks,
        double_z=True,
        z_channels=1,
        resolution=latent_dim,
        in_channels=1,
        out_ch=1,
        ch=1,
        embed_dim=1,
        act_fn=act_fn,
    )

    params = Autoencoder.init(rng, x=batch_autoenc, seed=rng_2)

    # Taking 64 images of the dataset
    batch_im = next(dset)
    # Generating new keys to use them for inference
    rng_1, rng_2 = jax.random.split(rng_2)

    # Initialisation
    optimizer = new_optimizer(
        FLAGS.opt, FLAGS.learning_rate, FLAGS.alpha, FLAGS.training_steps
    )
    opt_state = optimizer.init(params)

    def loglikelihood_fn(x, y, noise, type="Pixel"):
        stamp_size = x.shape[1]
        if type == "Fourier":
            print("in Fourier")
            xp = (
                jnp.fft.rfft2(x)
                / (jnp.sqrt(jnp.exp(noise)) + 0j)
                / stamp_size**2
                * (2 * jnp.pi) ** 2
            )
            yp = (
                jnp.fft.rfft2(y)
                / (jnp.sqrt(jnp.exp(noise)) + 0j)
                / stamp_size**2
                * (2 * jnp.pi) ** 2
            )
            return -0.5 * (jnp.abs(xp - yp) ** 2).sum()

        elif type == "Pixel":
            print("in pixels")
            # return - 0.5 * (jnp.abs(x - y)**2).sum() / noise**2
            return -0.5 * (jnp.abs(x - y) ** 2).sum() / 0.005**2

        else:
            raise NotImplementedError

    loglikelihood_fn = partial(loglikelihood_fn, type=FLAGS.noise)
    loglikelihood_fn = jax.vmap(loglikelihood_fn)

    @jax.jit
    def loss_fn(params, rng_key, batch, reg_term):  # state, rng_key, batch):
        """Function to define the loss function"""

        x = batch["image"]
        kpsf_real = batch["kpsf_real"]
        kpsf_imag = batch["kpsf_imag"]
        ps = batch["ps"]
        std = batch["noise_std"]

        kpsf = kpsf_real + 1j * kpsf_imag

        # Autoencode an example
        q, posterior, code = Autoencoder.apply(params, x=x, seed=rng_key)

        log_prob = posterior.log_prob(code)

        p = jax.vmap(convolve_kpsf)(q[..., 0], kpsf[..., 0])

        p = jnp.expand_dims(p, axis=-1)
        # p = q

        if FLAGS.noise == "Fourier":
            print("using the Fourier likelihood")
            log_likelihood = loglikelihood_fn(x, p, ps)
        elif FLAGS.noise == "Pixel":
            print("using the Pixel likelihood")
            log_likelihood = loglikelihood_fn(x, p, std)
        else:
            raise NotImplementedError

        print("log_likelihood", log_likelihood.shape)

        # KL divergence between the p(z|x) and p(z)
        prior = tfd.MultivariateNormalDiag(loc=jnp.zeros_like(code), scale_diag=[1.0])

        kl = (log_prob - prior.log_prob(code)).sum((-2, -1))

        # Calculating the ELBO value applying a regularization factor on the KL term
        elbo = log_likelihood - reg_term * kl

        print("ll", log_likelihood.shape)
        print("kl", kl.shape)
        print("elbo", elbo.shape)

        loss = -jnp.mean(elbo)

        return loss, -jnp.mean(log_likelihood)

    """    # Veryfing that the 'value_and_grad' works fine
    loss, grads = jax.value_and_grad(loss_fn)(params, rng, batch_im, kl_reg_w)
    """

    @jax.jit
    def update(params, rng_key, opt_state, batch):
        """Single SGD update step."""
        (loss, log_likelihood), grads = jax.value_and_grad(loss_fn, has_aux=True)(
            params, rng_key, batch, FLAGS.reg_value
        )
        updates, new_opt_state = optimizer.update(grads, opt_state, params)
        new_params = optax.apply_updates(params, updates)
        return loss, log_likelihood, new_params, new_opt_state

    """loss, log_likelihood, params, opt_state = update(params, rng_1, opt_state, batch_im)"""

    # Login to wandb
    wandb.login()

    # Initializing a Weights & Biases Run
    wandb.init(
        project=FLAGS.project,
        name=FLAGS.name,
        mode="offline",
        # tags="kl_reg={:.4f}".format(reg),
    )

    # Setting the configs of our experiment using `wandb.config`.
    # This way, Weights & Biases automatcally syncs the configs of
    # our experiment which could be used to reproduce the results of an experiment.
    config = wandb.config
    config.seed = 42
    config.batch_size = FLAGS.batch_size
    # config.validation_split = 0.2
    # config.pooling = "avg"
    config.learning_rate = FLAGS.learning_rate
    config.steps = FLAGS.training_steps
    config.kl_reg = FLAGS.reg_value
    config.using_kl = False if FLAGS.reg_value == 0 else True
    config.latent_dim = latent_dim
    # config.type_model = FLAGS.experiment
    # config.commit_version = get_git_commit_version()
    config.act_fn = FLAGS.act_fn
    config.opt = FLAGS.opt
    config.resnet_blocks = FLAGS.resblocks
    config.steps_schedule = FLAGS.step_sch
    config.scheduler = "Cosine Decay"
    config.interpolation = "Bicubic"
    config.noise_method = FLAGS.noise
    config.alpha = FLAGS.alpha

    # Define the metrics we are interested in the minimum of
    wandb.define_metric("loss", summary="min")
    wandb.define_metric("log_likelihood", summary="min")
    wandb.define_metric("test_loss", summary="min")
    wandb.define_metric("test_log_likelihood", summary="min")

    losses = []
    losses_test = []
    losses_test_step = []

    log_liks = []
    log_liks_test = []

    best_eval_loss = 1e6

    # Train the model as many steps as indicated initially
    for step in tqdm(range(1, config.steps + 1)):
        rng, rng_1 = random.split(rng)
        # Iterating over the dataset
        batch_im = next(dset)
        loss, log_likelihood, params, opt_state = update(
            params, rng_1, opt_state, batch_im
        )
        losses.append(loss)
        log_liks.append(log_likelihood)

        # Log metrics inside your training loop to visualize model performance
        wandb.log(
            {
                "loss": loss,
                "log_likelihood": log_likelihood,
            },
            step=step,
        )

        # Saving best checkpoint
        if loss < best_eval_loss:
            best_eval_loss = loss

            # if best_eval_loss < 0:
            save_checkpoint("checkpoint.msgpack", params, step)

        # Calculating the loss for all the test images
        if step % (config.steps // 50) == 0:
            dataset_eval = input_fn("test")
            test_iterator = dataset_eval.as_numpy_iterator()

            for_list_mean = []

            for img in test_iterator:
                rng, rng_1 = random.split(rng)
                loss_test, log_likelihood_test = loss_fn(
                    params, rng_1, img, FLAGS.reg_value
                )
                for_list_mean.append(loss_test)

            losses_test.append(np.mean(for_list_mean))
            losses_test_step.append(step)
            log_liks_test.append(log_likelihood_test)

            wandb.log(
                {
                    "test_loss": losses_test[-1],
                    "test_log_likelihood": log_liks_test[-1],
                },
                step=step,
            )

            print(
                "Step: {}, loss: {:.2f}, loss test: {:.2f}".format(
                    step, loss, losses_test[-1]
                )
            )

    # Loading checkpoint for the best step
    # params = load_checkpoint("checkpoint.msgpack", params)

    # Obtaining the step with the lowest loss value
    loss_min = min(losses)
    best_step = losses.index(loss_min) + 1
    print("\nBest Step: {}, loss: {:.2f}".format(best_step, loss_min))

    # Obtaining the step with the lowest log-likelihood value
    log_lik_min = min(log_liks)
    best_step_log = log_liks.index(log_lik_min) + 1
    print("\nBest Step: {}, log-likelihood: {:.2f}".format(best_step_log, log_lik_min))

    best_steps = {
        "best_step_loss": best_step,
        "best_step_log_lik": best_step_log,
    }

    wandb.log(best_steps)

    total_steps = np.arange(1, config.steps + 1)

    # Creating the 'results' folder to save all the plots as images (or validating that the folder already exists)
    results_folder = "results/{}".format(get_wandb_local_dir(wandb.run.dir))
    create_folder(results_folder)

    # Saving the loss plots
    save_plot_as_image(
        folder_path=results_folder,
        plot_title="Loglog of the Loss function - Train (KL reg value = {})".format(
            FLAGS.reg_value
        ),
        x_data=total_steps,
        y_data=losses,
        plot_type="loglog",
        file_name="loglog_loss.png",
    )
    save_plot_as_image(
        folder_path=results_folder,
        plot_title="Loglog of the Loss function - Test (KL reg value = {})".format(
            FLAGS.reg_value
        ),
        x_data=losses_test_step,
        y_data=losses_test,
        plot_type="loglog",
        file_name="loglog_loss_test.png",
    )

    # Saving the log-likelihood plots
    save_plot_as_image(
        folder_path=results_folder,
        plot_title="Loglog of the Log-likelihood - Train (KL reg value = {})".format(
            FLAGS.reg_value
        ),
        x_data=total_steps,
        y_data=log_liks,
        plot_type="loglog",
        file_name="loglog_log_likelihood.png",
    )
    save_plot_as_image(
        folder_path=results_folder,
        plot_title="Loglog of the Log-likelihood - Test (KL reg value = {})".format(
            FLAGS.reg_value
        ),
        x_data=losses_test_step,
        y_data=log_liks_test,
        plot_type="loglog",
        file_name="loglog_log_likelihood_test.png",
    )

    # Predicting over an example of data
    dataset_eval = input_fn("test")
    test_iterator = dataset_eval.as_numpy_iterator()
    batch = next(test_iterator)

    x = batch["image"]
    kpsf_real = batch["kpsf_real"]
    kpsf_imag = batch["kpsf_imag"]
    kpsf = kpsf_real + 1j * kpsf_imag

    # Taking 16 images as example
    batch = x[:16, ...]
    kpsf = kpsf[:16, ...]

    rng, rng_1 = random.split(rng)
    # X estimated distribution

    q, _, _ = Autoencoder.apply(params, x=batch, seed=rng_1)

    # Sample some variables from the posterior distribution
    rng, rng_1 = random.split(rng)

    p = jax.vmap(convolve_kpsf)(q[..., 0], kpsf[..., 0])

    p = jnp.expand_dims(p, axis=-1)

    min_value, max_value = norm_values_one_diff(batch, p, num_images=8)

    # Saving the samples of the predicted images and their difference from the original images
    # save_samples(folder_path=results_folder, decode=q, conv=p, batch=batch)
    save_samples(
        folder_path=results_folder,
        decode=q,
        conv=p,
        batch=batch,
        vmin=min_value,
        vmax=max_value,
    )

    wandb.finish()


if __name__ == "__main__":
    # Parse the command-line flags
    app.FLAGS(sys.argv)

    # Set the CUDA_VISIBLE_DEVICES environment variable
    os.environ["CUDA_VISIBLE_DEVICES"] = str(FLAGS.gpu)
    os.environ["XLA_FLAGS"] = "--xla_gpu_cuda_data_dir=/usr/local/cuda-12.1"
    os.environ["XLA_FLAGS"] = "--xla_gpu_force_compilation_parallelism=1"
    app.run(main)
