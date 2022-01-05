"""Example model training script."""

import hydra
import imgaug.augmenters as iaa
from omegaconf import DictConfig, ListConfig, OmegaConf
import os
import pytorch_lightning as pl
from sklearn import datasets
import torch
from typing import Callable, Union

from pose_est_nets.callbacks.callbacks import AnnealWeight
from pose_est_nets.datasets.datamodules import BaseDataModule, UnlabeledDataModule
from pose_est_nets.datasets.datasets import BaseTrackingDataset, HeatmapDataset
from pose_est_nets.models.heatmap_tracker import (
    HeatmapTracker,
    SemiSupervisedHeatmapTracker,
)
from pose_est_nets.models.regression_tracker import (
    RegressionTracker,
    SemiSupervisedRegressionTracker,
)
from pose_est_nets.utils.io import (
    return_absolute_data_paths,
    check_if_semi_supervised,
    format_and_update_loss_info,
)
from pose_est_nets.utils.plotting_utils import predict_dataset


_TORCH_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


def get_dataset(
    cfg: DictConfig, data_dir: str, imgaug_transform: iaa.Sequential
) -> Union[BaseTrackingDataset, HeatmapDataset]:

    if cfg.model.model_type == "regression":
        dataset = BaseTrackingDataset(
            root_directory=data_dir,
            csv_path=cfg.data.csv_file,
            header_rows=OmegaConf.to_object(cfg.data.header_rows),
            imgaug_transform=imgaug_transform,
        )
    elif cfg.model.model_type == "heatmap":
        dataset = HeatmapDataset(
            root_directory=data_dir,
            csv_path=cfg.data.csv_file,
            header_rows=OmegaConf.to_object(cfg.data.header_rows),
            imgaug_transform=imgaug_transform,
            downsample_factor=cfg.data.downsample_factor,
        )
    else:
        raise NotImplementedError(
            "%s is an invalid cfg.model.model_type" % cfg.model.model_type
        )
    return dataset


def get_imgaug_tranform(cfg: DictConfig) -> iaa.Sequential:
    # transforms
    data_transform = iaa.Resize(
        {
            "height": cfg.data.image_resize_dims.height,
            "width": cfg.data.image_resize_dims.width,
        }
    )
    return iaa.Sequential([data_transform])


def get_datamodule(
    cfg: DictConfig, dataset: Union[BaseTrackingDataset, HeatmapDataset], video_dir: str
) -> Union[BaseDataModule, UnlabeledDataModule]:

    semi_supervised = check_if_semi_supervised(cfg.model.losses_to_use)
    if not semi_supervised:
        if not (cfg.training.gpu_id, int):
            raise NotImplementedError(
                "Cannot currently fit fully supervised model on multiple gpus"
            )
        data_module = BaseDataModule(
            dataset=dataset,
            train_batch_size=cfg.training.train_batch_size,
            val_batch_size=cfg.training.val_batch_size,
            test_batch_size=cfg.training.test_batch_size,
            num_workers=cfg.training.num_workers,
            train_probability=cfg.training.train_prob,
            val_probability=cfg.training.val_prob,
            train_frames=cfg.training.train_frames,
            torch_seed=cfg.training.rng_seed_data_pt,
        )
    else:
        if not (cfg.training.gpu_id, int):
            raise NotImplementedError(
                "Cannot currently fit semi-supervised model on multiple gpus"
            )
        # copy data-specific details into loss dict
        # TODO: careful here, we may want to eliminate that when we refactor losses from datasets
        loss_param_dict, losses_to_use = format_and_update_loss_info(cfg)

        data_module = UnlabeledDataModule(
            dataset=dataset,
            video_paths_list=video_dir,
            losses_to_use=losses_to_use,
            loss_param_dict=loss_param_dict,
            train_batch_size=cfg.training.train_batch_size,
            val_batch_size=cfg.training.val_batch_size,
            test_batch_size=cfg.training.test_batch_size,
            num_workers=cfg.training.num_workers,
            train_probability=cfg.training.train_prob,
            val_probability=cfg.training.val_prob,
            train_frames=cfg.training.train_frames,
            unlabeled_batch_size=1,
            unlabeled_sequence_length=cfg.training.unlabeled_sequence_length,
            torch_seed=cfg.training.rng_seed_data_pt,
            dali_seed=cfg.training.rng_seed_data_dali,
            device_id=cfg.training.gpu_id,
        )
    return data_module


def get_model(
    cfg: DictConfig,
    data_module: Union[BaseDataModule, UnlabeledDataModule],
    loss_factory: Callable,  # TODO: be more specific
) -> Union[
    RegressionTracker,
    HeatmapTracker,
    SemiSupervisedRegressionTracker,
    SemiSupervisedHeatmapTracker,
]:
    semi_supervised = check_if_semi_supervised(cfg.model.losses_to_use)
    if not semi_supervised:
        if cfg.model.model_type == "regression":
            model = RegressionTracker(
                num_targets=cfg.data.num_targets,
                resnet_version=cfg.model.resnet_version,
                torch_seed=cfg.training.rng_seed_model_pt,
            )
        elif cfg.model.model_type == "heatmap":
            model = HeatmapTracker(
                num_targets=cfg.data.num_targets,
                resnet_version=cfg.model.resnet_version,
                downsample_factor=cfg.data.downsample_factor,
                supervised_loss=cfg.model.heatmap_loss_type,
                reach=cfg.model.reach,
                output_shape=data_module.dataset.output_shape,
                torch_seed=cfg.training.rng_seed_model_pt,
            )
        else:
            raise NotImplementedError(
                "%s is an invalid cfg.model.model_type for a fully supervised model"
                % cfg.model.model_type
            )

    else:  # semi_supervised == True
        # copy data-specific details into loss dict
        # TODO: in refactoring maybe change this
        loss_param_dict, losses_to_use = format_and_update_loss_info(cfg)

        if cfg.model.model_type == "regression":
            model = SemiSupervisedRegressionTracker(
                num_targets=cfg.data.num_targets,
                resnet_version=cfg.model.resnet_version,
                loss_params=data_module.loss_param_dict,
                semi_super_losses_to_use=losses_to_use,
                torch_seed=cfg.training.rng_seed_model_pt,
            )

        elif cfg.model.model_type == "heatmap":
            model = SemiSupervisedHeatmapTracker(
                num_targets=cfg.data.num_targets,
                resnet_version=cfg.model.resnet_version,
                downsample_factor=cfg.data.downsample_factor,
                supervised_loss=cfg.model.heatmap_loss_type,
                reach=cfg.model.reach,
                output_shape=data_module.dataset.output_shape,
                loss_params=data_module.loss_param_dict,
                semi_super_losses_to_use=losses_to_use,
                learn_weights=cfg.model.learn_weights,
                torch_seed=cfg.training.rng_seed_model_pt,
            )
        else:
            raise NotImplementedError(
                "%s is an invalid cfg.model.model_type for a semi-supervised model"
                % cfg.model.model_type
            )
        return model


@hydra.main(config_path="configs", config_name="config")
def train(cfg: DictConfig):
    """Main fitting function, accessed from command line."""

    print("Our Hydra config file:")
    print(cfg)

    # path handling for toy datasets
    data_dir, video_dir = return_absolute_data_paths(data_cfg=cfg.data)

    # imgaug transform
    imgaug_transform = get_imgaug_tranform(cfg=cfg)

    # dataset
    dataset = get_dataset(cfg=cfg, data_dir=data_dir, imgaug_transform=imgaug_transform)

    # datamodule
    data_module = get_datamodule(cfg=cfg, dataset=dataset, video_dir=video_dir)

    # losses
    # TODO

    # model
    # TODO: watch out for loss_factory argument when you introduce it
    model = get_model(cfg=cfg, data_module=data_module, loss_factory=None)

    # ----------------------------------------------------------------------------------
    # Set up and run training
    # ----------------------------------------------------------------------------------

    logger = pl.loggers.TensorBoardLogger("tb_logs", name=cfg.model.model_name)
    early_stopping = pl.callbacks.EarlyStopping(
        monitor="val_loss", patience=cfg.training.early_stop_patience, mode="min"
    )
    lr_monitor = pl.callbacks.LearningRateMonitor(logging_interval="epoch")
    ckpt_callback = pl.callbacks.model_checkpoint.ModelCheckpoint(monitor="val_loss")
    transfer_unfreeze_callback = pl.callbacks.BackboneFinetuning(
        unfreeze_backbone_at_epoch=cfg.training.unfreezing_epoch,
        lambda_func=lambda epoch: 1.5,
        backbone_initial_ratio_lr=0.1,
        should_align=True,
        train_bn=True,
    )
    anneal_weight_callback = AnnealWeight(**cfg.callbacks.anneal_weight)
    # TODO: add wandb?
    # determine gpu setup
    if _TORCH_DEVICE == "cpu":
        gpus = 0
    elif isinstance(cfg.training.gpu_id, list):
        gpus = cfg.training.gpu_id
    elif isinstance(cfg.training.gpu_id, ListConfig):
        gpus = list(cfg.training.gpu_id)
    elif isinstance(cfg.training.gpu_id, int):
        gpus = [cfg.training.gpu_id]
    else:
        raise NotImplementedError(
            "training.gpu_id must be list or int, not {}".format(
                type(cfg.training.gpu_id)
            )
        )
    trainer = pl.Trainer(  # TODO: be careful with devices when scaling to multiple gpus
        gpus=gpus,
        max_epochs=cfg.training.max_epochs,
        min_epochs=cfg.training.min_epochs,
        check_val_every_n_epoch=cfg.training.check_val_every_n_epoch,
        log_every_n_steps=cfg.training.log_every_n_steps,
        callbacks=[
            early_stopping,
            lr_monitor,
            ckpt_callback,
            transfer_unfreeze_callback,
            anneal_weight_callback,
        ],
        logger=logger,
        limit_train_batches=cfg.training.limit_train_batches,
        accumulate_grad_batches=cfg.training.accumulate_grad_batches,
        multiple_trainloader_mode=cfg.training.multiple_trainloader_mode,
        profiler=cfg.training.profiler,
    )
    trainer.fit(model=model, datamodule=data_module)

    # ----------------------------------------------------------------------------------
    # Post-training cleanup
    # ----------------------------------------------------------------------------------

    hydra_output_directory = os.getcwd()
    print("Hydra output directory: {}".format(hydra_output_directory))
    model_ckpt = trainer.checkpoint_callback.best_model_path
    model_ckpt_abs = os.path.abspath(model_ckpt)
    print("Best model path: {}".format(model_ckpt_abs))
    if not os.path.isfile(model_ckpt_abs):
        raise FileNotFoundError(
            "Cannot find model checkpoint. Have you trained for too few epochs?"
        )
    predict_dataset(
        cfg=cfg,
        data_module=data_module,
        hydra_output_directory=hydra_output_directory,
        ckpt_file=model_ckpt,
    )

    # generate a video
    # evaluate the network on everything in the video_dir, and make videos.


if __name__ == "__main__":
    train()
