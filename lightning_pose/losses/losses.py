"""Supervised and unsupervised losses implemented in pytorch.

The lightning pose package defines each loss as its own class; an initialized loss
object, in addition to computing the loss, stores hyperparameters related to the loss
(weight in the final objective funcion, epsilon-insensitivity parameter, etc.)

A separate LossFactory class (defined in lightning_pose.losses.factory) collects all
losses for a given model and orchestrates their execution, logging, etc.

The general flow of each loss class is as follows:
- input: predicted and ground truth data
- step 0: remove ground truth samples containing nans if desired
- step 1: compute loss for each batch element/keypoint/etc
- step 2: epsilon-insensitivity: set loss to zero for any batch element/keypoint with
          loss < epsilon
- step 3: reduce loss (usually mean)
- step 4: log values to a dict
- step 5: return weighted loss

"""

from geomloss import SamplesLoss
from omegaconf import ListConfig
import pytorch_lightning as pl
import torch
from torch.nn import functional as F
from torchtyping import TensorType, patch_typeguard
from typeguard import typechecked
from typing import Any, Callable, Dict, Tuple, List, Literal, Optional, Union

from lightning_pose.data.datamodules import BaseDataModule, UnlabeledDataModule
from lightning_pose.data.utils import generate_heatmaps
from lightning_pose.utils.pca import (
    KeypointPCA,
    format_multiview_data_for_pca,
    compute_pca_reprojection_error,
)

_TORCH_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

patch_typeguard()  # use before @typechecked


class Loss(pl.LightningModule):
    """Parent class for all losses."""

    def __init__(
        self,
        data_module: Optional[Union[BaseDataModule, UnlabeledDataModule]] = None,
        epsilon: float = 0.0,
        log_weight: float = 0.0,
        **kwargs,
    ) -> None:
        """

        Args:
            data_module: give losses access to data for computing data-specific loss
                params
            epsilon: loss values below epsilon will be zeroed out
            log_weight: natural log of the weight in front of the loss term in the final
                objective function

        """
        super().__init__()
        self.data_module = data_module
        self.epsilon = torch.tensor(epsilon, dtype=torch.float, device=self.device)
        self.log_weight = torch.tensor(
            log_weight, dtype=torch.float, device=self.device
        )
        self.loss_name = "base"

        self.reduce_methods_dict = {"mean": torch.mean, "sum": torch.sum}

    @property
    def weight(self) -> TensorType[()]:
        # weight = \sigma where our trainable parameter is \log(\sigma^2).
        # i.e., we take the parameter as it is in the config and exponentiate it to
        # enforce positivity
        weight = 1.0 / (2.0 * torch.exp(self.log_weight))
        return weight

    def remove_nans(self, **kwargs):
        # find nans in the targets, and do a masked_select operation
        raise NotImplementedError

    def compute_loss(self, **kwargs):
        raise NotImplementedError

    @typechecked
    def rectify_epsilon(self, loss: torch.Tensor) -> torch.Tensor:
        # loss values below epsilon as masked to zero
        loss = loss.masked_fill(mask=loss < self.epsilon, value=0.0)
        return loss

    @typechecked
    def reduce_loss(self, loss: torch.Tensor, method: str = "mean") -> TensorType[()]:
        return self.reduce_methods_dict[method](loss)

    @typechecked
    def log_loss(
        self,
        loss: torch.Tensor,
        stage: Literal["train", "val", "test"],
    ) -> List[dict]:
        loss_dict = {
            "name": "%s_%s_loss" % (stage, self.loss_name),
            "value": loss,
            "prog_bar": True,
        }
        weight_dict = {
            "name": "%s_weight" % self.loss_name,
            "value": self.weight,
        }
        return [loss_dict, weight_dict]

    def __call__(self, *args, **kwargs):
        # give us the flow of operations, and we overwrite the methods, and determine
        # their arguments which are in buffer

        # self.remove_nans()
        # self.compute_loss()
        # self.rectify_epsilon()
        # self.reduce_loss()
        # self.log_loss()

        # return self.weight * scalar_loss, logs
        raise NotImplementedError


class HeatmapLoss(Loss):
    """Parent class for different heatmap losses (MSE, Wasserstein, etc)."""

    @typechecked
    def __init__(
        self,
        data_module: Optional[Union[BaseDataModule, UnlabeledDataModule]] = None,
        log_weight: float = 0.0,
        **kwargs,
    ) -> None:
        super().__init__(data_module=data_module, log_weight=log_weight)

    @typechecked
    def remove_nans(
        self,
        targets: TensorType[
            "batch", "num_keypoints", "heatmap_height", "heatmap_width"
        ],
        predictions: TensorType[
            "batch", "num_keypoints", "heatmap_height", "heatmap_width"
        ],
    ) -> Tuple[
        TensorType["num_valid_keypoints", "heatmap_height", "heatmap_width"],
        TensorType["num_valid_keypoints", "heatmap_height", "heatmap_width"],
    ]:

        squeezed_targets = targets.reshape(targets.shape[0], targets.shape[1], -1)
        all_zeroes = torch.all(squeezed_targets == 0.0, dim=-1)

        return targets[~all_zeroes], predictions[~all_zeroes]

    def compute_loss(self, **kwargs):
        raise NotImplementedError

    @typechecked
    def __call__(
        self,
        heatmaps_targ: TensorType[
            "batch", "num_keypoints", "heatmap_height", "heatmap_width"
        ],
        heatmaps_pred: TensorType[
            "batch", "num_keypoints", "heatmap_height", "heatmap_width"
        ],
        stage: Optional[Literal["train", "val", "test"]] = None,
        **kwargs,
    ) -> Tuple[TensorType[()], List[dict]]:
        # give us the flow of operations, and we overwrite the methods, and determine
        # their arguments which are in buffer
        clean_targets, clean_predictions = self.remove_nans(
            targets=heatmaps_targ, predictions=heatmaps_pred
        )
        elementwise_loss = self.compute_loss(
            targets=clean_targets, predictions=clean_predictions
        )
        # epsilon_insensitive_loss = self.rectify_epsilon(loss=elementwise_loss)
        scalar_loss = self.reduce_loss(elementwise_loss, method="mean")
        logs = self.log_loss(loss=scalar_loss, stage=stage)

        return self.weight * scalar_loss, logs


class HeatmapMSELoss(HeatmapLoss):
    """MSE loss between heatmaps."""

    @typechecked
    def __init__(
        self,
        data_module: Optional[Union[BaseDataModule, UnlabeledDataModule]] = None,
        log_weight: float = 0.0,
        **kwargs,
    ) -> None:
        super().__init__(data_module=data_module, log_weight=log_weight)
        self.loss_name = "heatmap_mse"

    @typechecked
    def compute_loss(
        self,
        targets: TensorType["batch_x_num_keypoints", "heatmap_height", "heatmap_width"],
        predictions: TensorType[
            "batch_x_num_keypoints", "heatmap_height", "heatmap_width"
        ],
    ) -> TensorType["batch_x_num_keypoints", "heatmap_height", "heatmap_width"]:
        loss = F.mse_loss(targets, predictions, reduction="none")
        return loss


class HeatmapWassersteinLoss(HeatmapLoss):
    """Wasserstein loss between heatmaps."""

    @typechecked
    def __init__(
        self,
        data_module: Optional[Union[BaseDataModule, UnlabeledDataModule]] = None,
        log_weight: float = 0.0,
        reach: Union[float, str] = "none",
        **kwargs,
    ) -> None:
        super().__init__(data_module=data_module, log_weight=log_weight)
        reach_ = None if (reach == "none") else reach
        self.wasserstein_loss = SamplesLoss(loss="sinkhorn", reach=reach_)
        self.loss_name = "heatmap_wasserstein"

    @typechecked
    def compute_loss(
        self,
        targets: TensorType["num_valid_keypoints", "heatmap_height", "heatmap_width"],
        predictions: TensorType[
            "num_valid_keypoints", "heatmap_height", "heatmap_width"
        ],
    ) -> TensorType["num_valid_keypoints"]:
        return self.wasserstein_loss(targets, predictions)


class PCALoss(Loss):
    """Penalize predictions that fall outside a low-dimensional subspace."""

    @typechecked
    def __init__(
        self,
        loss_name: Literal["pca_singleview", "pca_multiview"],
        components_to_keep: Union[int, float] = 0.95,
        empirical_epsilon_percentile: float = 0.90,
        mirrored_column_matches: Optional[Union[ListConfig, List]] = None,
        data_module: Optional[Union[BaseDataModule, UnlabeledDataModule]] = None,
        log_weight: float = 0.0,
        device: str = _TORCH_DEVICE,
        **kwargs,
    ) -> None:
        super().__init__(data_module=data_module, log_weight=log_weight)
        self.loss_name = loss_name

        if loss_name == "pca_multiview":
            if mirrored_column_matches is None:
                raise ValueError("must provide mirrored_column_matches in data config")

        # initialize keypoint pca module
        self.pca = KeypointPCA(
            loss_type=self.loss_name,
            data_module=data_module,
            components_to_keep=components_to_keep,
            empirical_epsilon_percentile=empirical_epsilon_percentile,
            mirrored_column_matches=mirrored_column_matches,
            device=device,
        )
        # compute all the parameters needed for the loss
        self.pca()
        # empirically compute epsilon, already converted to tensor
        self.epsilon = self.pca.parameters["epsilon"]

    def remove_nans(self, **kwargs):
        # find nans in the targets, and do a masked_select operation
        pass

    @typechecked
    def compute_loss(
        self,
        predictions: TensorType["batch", "num_valid_dims"],
    ) -> TensorType["batch", "num_valid_dims_div_by_two"]:
        # compute reprojection error
        reproj_error = compute_pca_reprojection_error(
            clean_pca_arr=predictions,
            kept_eigenvectors=self.pca.parameters["kept_eigenvectors"],
            mean=self.pca.parameters["mean"],
        )
        return reproj_error

    @typechecked
    def __call__(
        self,
        keypoints_pred: torch.Tensor,
        stage: Optional[Literal["train", "val", "test"]] = None,
        **kwargs,
    ) -> Tuple[TensorType[()], List[dict]]:

        # if multiview, reformat the predictions first
        if self.loss_name == "pca_multiview":
            # shape = (batch, 2 * num_keypoints) where `num_keypoints` includes
            # keypoints views from multiple views
            keypoints_pred = keypoints_pred.reshape(
                keypoints_pred.shape[0], -1, 2
            )  # shape = (batch_size, num_keypoints, 2)
            keypoints_pred = format_multiview_data_for_pca(
                data_arr=keypoints_pred,
                mirrored_column_matches=self.pca.mirrored_column_matches,
            )  # shape = (batch_size * num_keypoints, views * 2)
        elif self.loss_name == "pca_singleview":
            # shape = (batch, 2 * num_keypoints)
            pass
        else:
            raise NotImplementedError

        elementwise_loss = self.compute_loss(predictions=keypoints_pred)
        epsilon_insensitive_loss = self.rectify_epsilon(loss=elementwise_loss)
        scalar_loss = self.reduce_loss(epsilon_insensitive_loss, method="mean")
        logs = self.log_loss(loss=scalar_loss, stage=stage)
        return self.weight * scalar_loss, logs


class TemporalLoss(Loss):
    """Penalize temporal differences for each target.

    Motion model: x_t = x_(t-1) + e_t, e_t ~ N(0, s)

    """

    @typechecked
    def __init__(
        self,
        data_module: Optional[Union[BaseDataModule, UnlabeledDataModule]] = None,
        epsilon: float = 0.0,
        log_weight: float = 0.0,
        **kwargs,
    ) -> None:
        super().__init__(
            data_module=data_module, epsilon=epsilon, log_weight=log_weight
        )
        self.loss_name = "temporal"

    def remove_nans(self, **kwargs):
        # find nans in the targets, and do a masked_select operation
        pass

    @typechecked
    def compute_loss(
        self,
        predictions: TensorType["batch", "two_x_num_keypoints"],
    ) -> TensorType["batch_minus_one", "num_keypoints"]:

        #  return shape: (batch - 1, num_targets)
        diffs = torch.diff(predictions, dim=0)

        # return shape: (batch - 1, num_keypoints, 2)
        reshape = torch.reshape(diffs, (diffs.shape[0], -1, 2))

        # return shape (batch - 1, num_keypoints)
        loss = torch.linalg.norm(reshape, ord=2, dim=2)

        return loss

    @typechecked
    def __call__(
        self,
        keypoints_pred: TensorType["batch", "two_x_num_keypoints"],
        stage: Optional[Literal["train", "val", "test"]] = None,
        **kwargs,
    ) -> Tuple[TensorType[()], List[dict]]:

        elementwise_loss = self.compute_loss(predictions=keypoints_pred)
        epsilon_insensitive_loss = self.rectify_epsilon(loss=elementwise_loss)
        scalar_loss = self.reduce_loss(epsilon_insensitive_loss, method="mean")
        logs = self.log_loss(loss=scalar_loss, stage=stage)
        return self.weight * scalar_loss, logs


class UnimodalLoss(Loss):
    """Encourage heatmaps to be unimodal using various measures."""

    @typechecked
    def __init__(
        self,
        loss_name: Literal["unimodal_mse", "unimodal_wasserstein"],
        original_image_height: int,
        original_image_width: int,
        downsampled_image_height: int,
        downsampled_image_width: int,
        reach: Union[float, str] = "none",
        data_module: Optional[Union[BaseDataModule, UnlabeledDataModule]] = None,
        epsilon: float = 0.0,
        log_weight: float = 0.0,
        **kwargs,
    ) -> None:

        super().__init__(
            data_module=data_module, epsilon=epsilon, log_weight=log_weight
        )

        self.loss_name = loss_name
        self.original_image_height = original_image_height
        self.original_image_width = original_image_width
        self.downsampled_image_height = downsampled_image_height
        self.downsampled_image_width = downsampled_image_width

        if self.loss_name == "unimodal_wasserstein":
            reach_ = None if (reach == "none") else reach
            self.wasserstein_loss = SamplesLoss(loss="sinkhorn", reach=reach_)
        else:
            self.wasserstein_loss = None

    def remove_nans(self, **kwargs):
        pass

    @typechecked
    def compute_loss(
        self,
        targets: TensorType[
            "batch", "num_keypoints", "heatmap_height", "heatmap_width"
        ],
        predictions: TensorType[
            "batch", "num_keypoints", "heatmap_height", "heatmap_width"
        ],
    ) -> torch.Tensor:

        if self.loss_name == "unimodal_mse":
            return F.mse_loss(targets, predictions, reduction="none")
        elif self.loss_name == "unimodal_wasserstein":
            # collapse over batch/keypoint dims
            targets_rs = targets.reshape(-1, targets.shape[-2], targets.shape[-1])
            predictions_rs = predictions.reshape(
                -1, predictions.shape[-2], predictions.shape[-1]
            )
            return self.wasserstein_loss(targets_rs, predictions_rs)
        else:
            raise NotImplementedError

    @typechecked
    def __call__(
        self,
        keypoints_pred: TensorType["batch", "two_x_num_keypoints"],
        heatmaps_pred: TensorType[
            "batch", "num_keypoints", "heatmap_height", "heatmap_width"
        ],
        stage: Optional[Literal["train", "val", "test"]] = None,
        **kwargs,
    ) -> Tuple[TensorType[()], List[dict]]:

        # turn keypoint predictions into unimodal heatmaps
        keypoints_pred = keypoints_pred.reshape(keypoints_pred.shape[0], -1, 2)
        heatmaps_ideal = generate_heatmaps(  # this process doesn't compute gradients
            keypoints=keypoints_pred,
            height=self.original_image_height,
            width=self.original_image_width,
            output_shape=(self.downsampled_image_height, self.downsampled_image_width),
        )

        # compare unimodal heatmaps with predicted heatmaps
        elementwise_loss = self.compute_loss(
            targets=heatmaps_ideal, predictions=heatmaps_pred
        )
        scalar_loss = self.reduce_loss(elementwise_loss, method="mean")
        logs = self.log_loss(loss=scalar_loss, stage=stage)

        return self.weight * scalar_loss, logs


class RegressionMSELoss(Loss):
    """MSE loss between ground truth and predicted coordinates."""

    @typechecked
    def __init__(
        self,
        data_module: Optional[Union[BaseDataModule, UnlabeledDataModule]] = None,
        epsilon: float = 0.0,
        log_weight: float = 0.0,
        **kwargs,
    ) -> None:
        super().__init__(
            data_module=data_module, epsilon=epsilon, log_weight=log_weight
        )
        self.loss_name = "regression_mse"

    @typechecked
    def remove_nans(
        self,
        targets: TensorType["batch", "two_x_num_keypoints"],
        predictions: TensorType["batch", "two_x_num_keypoints"],
    ) -> Tuple[TensorType["num_valid_keypoints"], TensorType["num_valid_keypoints"],]:
        mask = targets == targets  # keypoints is not none, bool
        targets_masked = torch.masked_select(targets, mask)
        predictions_masked = torch.masked_select(predictions, mask)
        return targets_masked, predictions_masked

    @typechecked
    def compute_loss(
        self,
        targets: TensorType["batch_x_two_x_num_keypoints"],
        predictions: TensorType["batch_x_two_x_num_keypoints"],
    ) -> TensorType["batch_x_two_x_num_keypoints"]:
        loss = F.mse_loss(targets, predictions, reduction="none")
        return loss

    @typechecked
    def __call__(
        self,
        keypoints_targ: TensorType["batch", "two_x_num_keypoints"],
        keypoints_pred: TensorType["batch", "two_x_num_keypoints"],
        stage: Optional[Literal["train", "val", "test"]] = None,
        **kwargs,
    ) -> Tuple[TensorType[()], List[dict]]:

        clean_targets, clean_predictions = self.remove_nans(
            targets=keypoints_targ, predictions=keypoints_pred
        )
        elementwise_loss = self.compute_loss(
            targets=clean_targets, predictions=clean_predictions
        )
        scalar_loss = self.reduce_loss(elementwise_loss, method="mean")
        logs = self.log_loss(loss=scalar_loss, stage=stage)

        return self.weight * scalar_loss, logs


class RegressionRMSELoss(RegressionMSELoss):
    """Root MSE loss between ground truth and predicted coordinates."""

    @typechecked
    def __init__(
        self,
        data_module: Optional[Union[BaseDataModule, UnlabeledDataModule]] = None,
        epsilon: float = 0.0,
        log_weight: float = 0.0,
        **kwargs,
    ) -> None:
        super().__init__(
            data_module=data_module, epsilon=epsilon, log_weight=log_weight
        )
        self.loss_name = "rmse"

    @typechecked
    def compute_loss(
        self,
        targets: TensorType["batch_x_two_x_num_keypoints"],
        predictions: TensorType["batch_x_two_x_num_keypoints"],
    ) -> TensorType["batch_x_num_keypoints"]:
        targs = targets.reshape(-1, 2)
        preds = predictions.reshape(-1, 2)
        loss = torch.mean(F.mse_loss(targs, preds, reduction="none"), dim=1)
        return torch.sqrt(loss)


@typechecked
def get_loss_classes() -> Dict[str, Callable]:
    """Get a dict with all the loss classes.

    Returns:
        Dict[str, Callable]: [description]

    """
    loss_dict = {
        "regression": RegressionMSELoss,
        "heatmap_mse": HeatmapMSELoss,
        "heatmap_wasserstein": HeatmapWassersteinLoss,
        "pca_multiview": PCALoss,
        "pca_singleview": PCALoss,
        "temporal": TemporalLoss,
        "unimodal_mse": UnimodalLoss,
        "unimodal_wasserstein": UnimodalLoss,
    }
    return loss_dict
