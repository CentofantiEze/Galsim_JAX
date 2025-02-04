import os
import matplotlib.pyplot as plt
import jax.numpy as jnp
import subprocess
import wandb
import optax
import numpy as np

from flax.serialization import to_state_dict, msgpack_serialize, from_bytes
from flax import linen as nn  # Linen API


def create_folder(folder_path="results"):
    try:
        # Create folder if it doesn't exist
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)
            print(f"Folder created: {folder_path}")
        else:
            # Folder already exists
            print("Folder already exists!")
    except Exception as e:
        print(f"Error creating folder: {str(e)}")


def lr_schedule(step):
    """Linear scaling rule optimized for 90 epochs."""
    steps_per_epoch = 12000 // 28

    current_epoch = step / steps_per_epoch  # type: float
    boundaries = jnp.array((40, 100, 160)) * steps_per_epoch
    values = jnp.array([1.0, 0.1, 0.01, 0.001])

    index = jnp.sum(boundaries < step)
    return jnp.take(values, index)


def get_git_commit_version():
    """ "Allows to get the Git commit version to tag each experiment"""
    try:
        commit_version = (
            subprocess.check_output(["git", "rev-parse", "HEAD"])
            .decode("utf-8")
            .strip()
        )
        return commit_version
    except subprocess.CalledProcessError:
        return None


def save_checkpoint(ckpt_path, state, step):
    """Saves a Wandb checkpoint."""
    with open(ckpt_path, "wb") as outfile:
        outfile.write(msgpack_serialize(to_state_dict(state)))
    artifact = wandb.Artifact(f"{wandb.run.id}-checkpoint", type="model")
    artifact.add_file(ckpt_path)
    wandb.log_artifact(
        # artifact, aliases=["best", f"step_{step}", f"commit_{get_git_commit_version()}"]
        artifact, aliases=["best", f"step_{step}"]
    )


def load_checkpoint(ckpt_file, state):
    """Loads the best Wandb checkpoint."""
    artifact = wandb.use_artifact(f"{wandb.run.id}-checkpoint:best")
    artifact_dir = artifact.download()
    ckpt_path = os.path.join(artifact_dir, ckpt_file)
    with open(ckpt_path, "rb") as data_file:
        byte_data = data_file.read()
    return from_bytes(state, byte_data)


def save_plot_as_image(
    folder_path,
    plot_title,
    x_data,
    y_data,
    plot_type="loglog",
    file_name="plot.png",
    **kwargs,
):
    # Generate plot based on plot_type
    if plot_type == "line":
        plt.plot(x_data, y_data, **kwargs)
    elif plot_type == "loglog":
        plt.loglog(x_data, y_data, **kwargs)
    elif plot_type == "semilogy":
        plt.semilogy(x_data, y_data, **kwargs)
    elif plot_type == "semilogx":
        plt.semilogx(x_data, y_data, **kwargs)
    elif plot_type == "scatter":
        plt.scatter(x_data, y_data, **kwargs)
    else:
        print("Invalid plot type!")
        return

    plt.title(plot_title)
    plt.xlabel("Step")
    plt.ylabel("Value")

    # Save plot as image within the folder
    file_path = os.path.join(folder_path, file_name)
    plt.savefig(file_path)
    wandb.log({"{}".format(file_name.split(".")[0]): wandb.Image(plt)})
    plt.close()

    print(f"Plot saved as {file_path}")


def save_samples(folder_path, decode, conv, batch, vmin, vmax):
    # Plotting the original, predicted and their differences for 8 examples
    num_rows, num_cols = 8, 4

    plt.figure(figsize=(12, 24))
    fig, axes = plt.subplots(num_rows, num_cols, figsize=(12, 24))

    for i, (ax1, ax2, ax3, ax4) in enumerate(
        zip(axes[:, 0], axes[:, 1], axes[:, 2], axes[:, 3])
    ):
        batch_img = batch[i, ...]
        decode_img = decode[i, ...]
        conv_img = conv[i, ...]

        # Plotting original image
        ax1.imshow(batch_img.mean(axis=-1))
        ax1.axis("off")
        # Plotting predicted image
        ax2.imshow(decode_img.mean(axis=-1))
        ax2.axis("off")
        # Plotting predicted convolved image
        ax3.imshow(conv_img.mean(axis=-1))
        ax3.axis("off")
        # Plotting difference between original and predicted image
        ax4.imshow(
            conv_img.mean(axis=-1) - batch_img.mean(axis=-1), vmin=vmin, vmax=vmax
        )
        ax4.axis("off")

    # Add a title to the figure
    fig.suptitle(
        "Comparison between original and predicted images", fontsize=12, y=0.99
    )

    # Adjust the layout of the subplots
    fig.tight_layout()

    # Save plot as image within the folder
    file_path = os.path.join(folder_path, "difference_pred.png")
    plt.savefig(file_path)
    wandb.log({"difference_pred": wandb.Image(plt)})
    plt.close(fig)

    print(f"Plot saved as {file_path}")

    # Plotting 16 images of the estimated shape of galaxies
    num_rows, num_cols = 4, 4

    fig, axes = plt.subplots(num_rows, num_cols, figsize=(10, 10))

    for ax, conv_img in zip(axes.flatten(), conv):
        ax.imshow(conv_img.mean(axis=-1))
        ax.axis("off")

    # Add a title to the figure
    fig.suptitle("Samples of predicted galaxies", fontsize=16)

    # Adjust the layout of the subplots
    fig.tight_layout()
    # Save plot as image within the folder
    file_path = os.path.join(folder_path, "samples_pred.png")
    plt.savefig(file_path)
    wandb.log({"samples_pred": wandb.Image(plt)})
    plt.close(fig)

    print(f"Plot saved as {file_path}")


def get_wandb_local_dir(wandb_local_dir):
    # Extract the substring between 'run-' and '/files'
    start_index = wandb_local_dir.find("wandb/") + len("wandb/")
    end_index = wandb_local_dir.find("/files")
    run_string = wandb_local_dir[start_index:end_index]

    return run_string


def get_activation_fn(name):
    """JAX built-in activation functions"""

    activation_functions = {
        "linear": lambda: lambda x: x,
        "relu": nn.relu,
        "relu6": nn.relu6,
        "elu": nn.elu,
        "gelu": nn.gelu,
        "prelu": nn.PReLU,
        "leaky_relu": nn.leaky_relu,
        "hardtanh": nn.hard_tanh,
        "sigmoid": nn.sigmoid,
        "tanh": nn.tanh,
        "log_sigmoid": nn.log_sigmoid,
        "softplus": nn.softplus,
        "softsign": nn.soft_sign,
        "swish": nn.swish,
    }

    if name not in activation_functions:
        raise ValueError(
            f"'{name}' is not included in activation_functions. use below one. \n {activation_functions.keys()}"
        )

    return activation_functions[name]


def get_optimizer(name, lr, num_steps):
    """JAX built-in activation functions"""

    optimizer = {
        "adam": optax.chain(optax.adam(lr), optax.scale_by_schedule(lr_schedule)),
        "adamw": optax.chain(optax.adamw(lr), optax.scale_by_schedule(lr_schedule)),
        "adafactor": optax.chain(
            optax.adafactor(lr), optax.scale_by_schedule(lr_schedule)
        ),
    }

    if name not in optimizer:
        raise ValueError(
            f"'{name}' is not included in optimizer names. use below one. \n {optimizer.keys()}"
        )

    return optimizer[name]


def new_optimizer(name, init_lr, alpha, total_steps):
    schedule = optax.cosine_decay_schedule(
        init_lr, decay_steps=total_steps, alpha=alpha
    )

    optimizer = {
        "adam": optax.adam(learning_rate=schedule),
        "adamw": optax.adamw(learning_rate=schedule),
        "adafactor": optax.adafactor(learning_rate=schedule),
    }

    if name not in optimizer:
        raise ValueError(
            f"'{name}' is not included in optimizer names. use below one. \n {optimizer.keys()}"
        )

    return optimizer[name]


def norm_values_one_diff(orig, inf1, num_images=8):
    min_values = []
    max_values = []

    for i in range(num_images):
        orig_img = orig[i, ...]
        inf1_img = inf1[i, ...]

        diff_1 = inf1_img - orig_img

        min_values.append(diff_1.mean(axis=-1).min())
        max_values.append(diff_1.mean(axis=-1).max())

    return [np.min(min_values), np.max(max_values)]


def plot_examples(images, plt_title, plt_label, wandb_name):
    # Plotting the original, predicted and their differences for 8 examples
    num_rows, num_cols = 4, 8

    plt.figure(figsize=(14.5, 8))

    fig, axes = plt.subplots(num_rows, num_cols, figsize=(14.5, 8))

    for ax, z_img in zip(axes.flatten(), images):
        ax.imshow(z_img)
        ax.axis("off")

    # Add a title to the figure
    fig.suptitle(plt_title, fontsize=12, y=0.99)

    # Adjust the layout of the subplots
    fig.tight_layout()

    cb_ax = fig.add_axes([1.005, 0.03, 0.015, 0.90])
    fig.colorbar(ax.imshow(z_img), label=plt_label, orientation="vertical", cax=cb_ax)
    wandb.log({wandb_name: wandb.Image(plt)})
    plt.close(fig)


def load_checkpoint_wandb(wandb_id, ckpt_file, state):
    """Loads the best Wandb checkpoint."""
    artifact_dir = f"artifacts/{wandb_id}-checkpoint:best/"
    ckpt_path = os.path.join(artifact_dir, ckpt_file)
    with open(ckpt_path, "rb") as data_file:
        byte_data = data_file.read()
    return from_bytes(state, byte_data)


# def lr_storage(init_lr, total_steps, alpha, epoch):
#     schedule = optax.cosine_decay_schedule(
#         init_lr, decay_steps=total_steps, alpha=alpha
#     )

#     return schedule(epoch)
