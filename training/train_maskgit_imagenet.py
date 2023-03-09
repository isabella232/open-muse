import logging
import os
import time
from pathlib import Path
from typing import Any, List, Tuple

import datasets
import torch
import torch.nn.functional as F
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import set_seed
from omegaconf import DictConfig, ListConfig, OmegaConf
from torch.optim import AdamW  # why is shampoo not available in PT :(
from torchvision import transforms

import muse
from muse import MaskGitTransformer, MaskGitVQGAN
from muse.lr_schedulers import get_scheduler
from muse.sampling import cosine_schedule

logger = get_logger(__name__, log_level="INFO")

dataset_name_mapping = {
    "imagenet-1k": ("image", "label"),
    "cifar10": ("img", "label"),
}


def get_config():
    cli_conf = OmegaConf.from_cli()

    yaml_conf = OmegaConf.load(cli_conf.config)
    conf = OmegaConf.merge(yaml_conf, cli_conf)

    return conf


def flatten_omega_conf(cfg: Any, resolve: bool = False) -> List[Tuple[str, Any]]:
    ret = []

    def handle_dict(key: Any, value: Any, resolve: bool) -> List[Tuple[str, Any]]:
        return [(f"{key}.{k1}", v1) for k1, v1 in flatten_omega_conf(value, resolve=resolve)]

    def handle_list(key: Any, value: Any, resolve: bool) -> List[Tuple[str, Any]]:
        return [(f"{key}.{idx}", v1) for idx, v1 in flatten_omega_conf(value, resolve=resolve)]

    if isinstance(cfg, DictConfig):
        for k, v in cfg.items_ex(resolve=resolve):
            if isinstance(v, DictConfig):
                ret.extend(handle_dict(k, v, resolve=resolve))
            elif isinstance(v, ListConfig):
                ret.extend(handle_list(k, v, resolve=resolve))
            else:
                ret.append((str(k), v))
    elif isinstance(cfg, ListConfig):
        for idx, v in enumerate(cfg._iter_ex(resolve=resolve)):
            if isinstance(v, DictConfig):
                ret.extend(handle_dict(idx, v, resolve=resolve))
            elif isinstance(v, ListConfig):
                ret.extend(handle_list(idx, v, resolve=resolve))
            else:
                ret.append((str(idx), v))
    else:
        assert False

    return ret


def main():
    config = get_config()

    config.experiment.logging_dir = Path(config.experiment.output_dir) / "logs"
    accelerator = Accelerator(
        gradient_accumulation_steps=config.training.gradient_accumulation_steps,
        mixed_precision=config.training.mixed_precision,
        log_with="wandb",
        logging_dir=config.experiment.logging_dir,
    )

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.info(accelerator.state, main_process_only=False)
    if accelerator.is_local_main_process:
        datasets.utils.logging.set_verbosity_warning()
        muse.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        muse.logging.set_verbosity_error()

    if accelerator.is_main_process:
        os.makedirs(config.experiment.output_dir, exist_ok=True)
        config_path = Path(config.experiment.output_dir) / "config.yaml"
        logging.info(f"Saving config to {config_path}")
        OmegaConf.save(config, config_path)

    # If passed along, set the training seed now.
    if config.training.seed is not None:
        set_seed(config.training.seed)

    #########################
    # MODELS and OPTIMIZER  #
    #########################
    logger.info("Loading models and optimizer")
    vq_model = MaskGitVQGAN.from_pretrained(config.model.vq_model.pretrained)
    model = MaskGitTransformer(**config.model.transformer)
    mask_id = model.config.mask_token_id

    # Freeze the VQGAN
    vq_model.requires_grad_(False)

    # create custom saving & loading hooks so that `accelerator.save_state(...)` serializes in a nice format
    def save_model_hook(models, weights, output_dir):
        for model in models:
            model.save_pretrained(output_dir)
            # make sure to pop weight so that corresponding model is not saved again
            weights.pop()

    def load_model_hook(models, input_dir):
        while len(models) > 0:
            # pop models so that they are not loaded again
            model = models.pop()

            # load muse style into model
            load_model = MaskGitTransformer.from_pretrained(input_dir)
            model.register_to_config(**load_model.config)

            model.load_state_dict(load_model.state_dict())
            del load_model

    accelerator.register_save_state_pre_hook(save_model_hook)
    accelerator.register_load_state_pre_hook(load_model_hook)

    optimizer_config = config.optimizer.params
    learning_rate = optimizer_config.learning_rate
    if optimizer_config.scale_lr:
        learning_rate = (
            learning_rate
            * config.training.batch_size
            * accelerator.num_processes
            * config.training.gradient_accumulation_steps
        )

    optimizer = AdamW(
        model.parameters(),
        lr=optimizer_config.learning_rate,
        betas=(optimizer_config.beta1, optimizer_config.beta2),
        weight_decay=optimizer_config.weight_decay,
        eps=optimizer_config.epsilon,
    )

    ##################################
    # DATSET LOADING & PREPROCESSING #
    #################################
    # Get the datasets: you can either provide your own training and evaluation files (see below)
    # or specify a Dataset from the hub (the dataset will be downloaded automatically from the datasets Hub).

    # In distributed training, the load_dataset function guarantees that only one local process can concurrently
    # download the dataset.
    logger.info("Loading datasets")
    datasets_config = config.dataset.params
    dataset = datasets.load_dataset(datasets_config.path, streaming=datasets_config.streaming, use_auth_token=True)
    dataset = dataset.with_format("torch")

    # Preprocessing the datasets.

    # 6. Get the column names for input/target.
    if datasets_config.streaming:
        column_names = list(next(iter(dataset["train"])).keys())
    else:
        column_names = dataset["train"].column_names
    dataset_columns = dataset_name_mapping.get(datasets_config.path, None)
    if datasets_config.image_column is None:
        image_column = dataset_columns[0] if dataset_columns is not None else column_names[0]
    else:
        image_column = datasets_config.image_column
        if image_column not in column_names:
            raise ValueError(
                f"--image_column' value '{datasets_config.image_column}' needs to be one of: {', '.join(column_names)}"
            )
    if datasets_config.label_column is None:
        label_column = dataset_columns[1] if dataset_columns is not None else column_names[1]
    else:
        label_column = datasets_config.label_column
        if label_column not in column_names:
            raise ValueError(
                f"--label_column' value '{datasets_config.label_column}' needs to be one of: {', '.join(column_names)}"
            )

    preproc_config = config.dataset.preprocessing
    train_transforms = transforms.Compose(
        [
            transforms.Resize(preproc_config.resolution, interpolation=transforms.InterpolationMode.BILINEAR),
            (
                transforms.CenterCrop(preproc_config.resolution)
                if preproc_config.center_crop
                else transforms.RandomCrop(preproc_config.resolution)
            ),
            transforms.RandomHorizontalFlip() if preproc_config.random_flip else transforms.Lambda(lambda x: x),
            transforms.ToTensor(),
        ]
    )
    eval_transforms = transforms.Compose(
        [
            transforms.Resize(preproc_config.resolution, interpolation=transforms.InterpolationMode.BILINEAR),
            transforms.CenterCrop(preproc_config.resolution),
            transforms.ToTensor(),
        ]
    )

    def train_preprocess(examples):
        examples[image_column] = [train_transforms(image.convert("RGB")) for image in examples[image_column]]
        return examples

    def eval_preprocess(examples):
        examples[image_column] = [eval_transforms(image.convert("RGB")) for image in examples[image_column]]
        return examples

    logger.info("Preprocessing and shuffling datasets.")
    with accelerator.main_process_first():
        # Set the training transforms
        eval_split_name = "validation" if "validation" in dataset else "test"
        if eval_split_name not in dataset and not datasets_config.streaming:
            dataset = dataset["train"].train_test_split(test_size=0.1)

        train_dataset = dataset["train"]
        eval_dataset = dataset[eval_split_name]

        if datasets_config.streaming:
            train_dataset = train_dataset.map(train_preprocess, batched=True)
            eval_dataset = eval_dataset.map(eval_preprocess, batched=True)
        else:
            train_dataset = train_dataset.with_transform(train_preprocess)
            eval_dataset = eval_dataset.with_transform(eval_preprocess)

        # We need to shuffle early when using streaming datasets
        if datasets_config.streaming:
            train_dataset = train_dataset.shuffle(
                buffer_size=datasets_config.shuffle_buffer_size, seed=config.training.seed
            )

    def collate_fn(examples):
        pixel_values = torch.stack([example[image_column] for example in examples])
        pixel_values = pixel_values.float()
        class_id = torch.tensor([example[label_column] for example in examples])
        return {"pixel_values": pixel_values, "class_id": class_id}

    ##################################
    # DATLOADER and LR-SCHEDULER     #
    #################################
    # DataLoaders creation:
    logger.info("Creating dataloaders and lr_scheduler")
    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        collate_fn=collate_fn,
        batch_size=config.training.batch_size,
        num_workers=datasets_config.workers,
        shuffle=not datasets_config.streaming,
        drop_last=True,
    )
    eval_dataloader = torch.utils.data.DataLoader(
        eval_dataset,
        collate_fn=collate_fn,
        batch_size=config.training.batch_size,
        drop_last=True,
    )

    total_batch_size = (
        config.training.batch_size * accelerator.num_processes * config.training.gradient_accumulation_steps
    )
    lr_scheduler = get_scheduler(
        config.lr_scheduler.scheduler,
        optimizer=optimizer,
        num_warmup_steps=config.lr_scheduler.params.warmup_steps * config.training.gradient_accumulation_steps,
    )

    # Prepare everything with accelerator
    logger.info("Preparing model, optimizer and dataloaders")
    model, optimizer, train_dataloader, eval_dataloader, lr_scheduler = accelerator.prepare(
        model, optimizer, train_dataloader, eval_dataloader, lr_scheduler
    )

    vq_model.to(accelerator.device)

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if accelerator.is_main_process:
        accelerator.init_trackers(
            config.experiment.project, config={k: v for k, v in flatten_omega_conf(config, resolve=True)}
        )

    # TODO: Add checkpoint loading. We need to check how to resume datasets in streaming mode.

    if config.training.overfit_one_batch:
        train_dataloader = [next(iter(train_dataloader))]

    # Train!
    logger.info("***** Running training *****")
    logger.info(f"  Num Epochs = {config.training.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = { config.training.batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {config.training.gradient_accumulation_steps}")
    global_step = 0
    first_epoch = 0
    examples_since_last_logged = 0

    def train_step(batch, log_first_batch=False):
        with accelerator.accumulate(model):
            # TODO(Patrick) - We could definitely pre-compute the image tokens for faster training on larger datasets
            with torch.no_grad():
                image_tokens = vq_model.encode(batch["pixel_values"])[1]

            batch_size, seq_len = image_tokens.shape

            # TODO(Patrick) - I don't think that's how the timesteps are sampled in maskgit or MUSE

            # Sample a random timestep for each image
            timesteps = torch.rand(batch_size, device=image_tokens.device)
            # Sample a random mask probability for each image using timestep and cosine schedule
            mask_prob = cosine_schedule(timesteps)
            mask_prob = mask_prob.clip(config.training.min_masking_rate)
            # creat a random mask for each image

            num_token_masked = (seq_len * mask_prob).round().clamp(min=1)

            batch_randperm = torch.rand(batch_size, seq_len, device=image_tokens.device).argsort(dim=-1)
            mask = batch_randperm < num_token_masked.unsqueeze(-1)
            # mask images and create input and labels
            input_ids = torch.where(mask, mask_id, image_tokens)
            labels = torch.where(mask, image_tokens, -100)

            # shift the class ids by codebook size
            class_ids = batch["class_id"] + vq_model.num_embeddings
            # prepend the class ids to the image tokens
            input_ids = torch.cat([class_ids.unsqueeze(-1), input_ids], dim=-1)
            # prepend -100 to the labels as we don't want to predict the class ids
            labels = torch.cat([-100 * torch.ones_like(class_ids).unsqueeze(-1), labels], dim=-1)

            # log the inputs for the first step of the first epoch
            if log_first_batch:
                logger.info("Input ids: {}".format(input_ids))
                logger.info("Labels: {}".format(labels))

            _, loss = model(input_ids=input_ids, labels=labels)

            # Gather thexd losses across all processes for logging (if we use distributed training).
            avg_loss = accelerator.gather(loss.repeat(config.training.batch_size)).mean()
            avg_masking_rate = accelerator.gather(mask_prob.repeat(config.training.batch_size)).mean()
            return loss, avg_loss, avg_masking_rate

    @torch.no_grad()
    def eval_step(batch):
        _, avg_loss, _ = train_step(batch)
        return avg_loss

    now = time.time()
    for epoch in range(first_epoch, config.training.num_train_epochs):
        model.train()
        if datasets_config.streaming and not config.training.overfit_one_batch:
            train_dataset.set_epoch(epoch)
        for batch in train_dataloader:
            loss, avg_loss, avg_masking_rate = train_step(batch, log_first_batch=global_step == 0 and epoch == 0)

            accelerator.backward(loss)
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad(set_to_none=True)

            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                examples_since_last_logged += (
                    batch["pixel_values"].shape[0] * config.training.gradient_accumulation_steps
                )

                if global_step % config.experiment.log_every == 0:
                    images_per_second_per_gpu = examples_since_last_logged / (time.time() - now)
                    # Log metrics
                    logs = {
                        "step_loss": avg_loss.item(),
                        "lr": lr_scheduler.get_last_lr()[0],
                        "avg_masking_rate": avg_masking_rate.item(),
                        "images/sec/gpu": images_per_second_per_gpu,
                    }
                    accelerator.log(logs, step=global_step)

                    logger.info(
                        f" Step: {global_step} Loss: {avg_loss.item():0.4f} im/s/GPU: {images_per_second_per_gpu:0.2f}"
                    )

                if global_step % config.experiment.eval_every == 0:
                    logger.info("Evaluating...")
                    model.eval()
                    eval_loss = 0
                    num_eval_examples = 0
                    now = time.time()
                    for i, batch in enumerate(eval_dataloader):
                        eval_loss += eval_step(batch)
                        num_eval_examples += batch["pixel_values"].shape[0]
                        if num_eval_examples >= config.experiment.max_eval_examples:
                            break
                    eval_loss = eval_loss / (i + 1)
                    accelerator.log({"eval_loss": eval_loss.item()}, step=global_step)

                    eval_time = time.time() - now
                    if epoch == 0 and global_step == 0:
                        logger.info(f"First eval time: {eval_time} seconds")

                    logger.info(f"Step: {global_step} Eval Loss: {eval_loss.item():0.4f}")
                    model.train()

                if global_step % config.experiment.save_every == 0:
                    if accelerator.is_main_process:
                        save_path = Path(config.experiment.output_dir) / f"checkpoint-{global_step}"
                        accelerator.save_state(save_path)
                        logger.info(f"Saved state to {save_path}")
                global_step += 1
                # TODO: Add generation

    # Save the final trained checkpoint
    accelerator.wait_for_everyone()
    if accelerator.is_main_process:
        model = accelerator.unwrap_model(model)
        model.save_pretrained(config.experiment.output_dir)

    accelerator.end_training()


if __name__ == "__main__":
    main()
