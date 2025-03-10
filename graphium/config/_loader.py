from typing import Dict, Mapping, Tuple, Type, Union, Any, Optional, Callable

# Misc
import os
import omegaconf
from copy import deepcopy
from loguru import logger
import yaml
import joblib
import pathlib
import warnings

# Torch
import torch
import mup

# Lightning
from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint
from pytorch_lightning import Trainer
from pytorch_lightning.loggers import WandbLogger, Logger

# Graphium
from graphium.utils.mup import set_base_shapes
from graphium.ipu.ipu_dataloader import IPUDataloaderOptions
from graphium.trainer.metrics import MetricWrapper
from graphium.nn.architectures import FullGraphMultiTaskNetwork
from graphium.nn.utils import MupMixin
from graphium.trainer.predictor import PredictorModule
from graphium.utils.spaces import DATAMODULE_DICT
from graphium.ipu.ipu_utils import import_poptorch, load_ipu_options
from graphium.data.datamodule import MultitaskFromSmilesDataModule, BaseDataModule

# Weights and Biases
from pytorch_lightning import Trainer


def get_accelerator(
    config_acc: Union[omegaconf.DictConfig, Dict[str, Any]],
) -> str:
    """
    Get the accelerator from the config file, and ensure that they are
    consistant. For example, specifying `cpu` as the accelerators, but
    `gpus>0` as a Trainer option will yield an error.
    """

    # Get the accelerator type
    accelerator_type = config_acc["type"]

    # Get the GPU info
    gpus = config_acc["config_override"].get("trainer", {}).get("trainer", {}).get("gpus", 0)
    if gpus > 0:
        assert (accelerator_type is None) or (accelerator_type == "gpu"), "Accelerator mismatch"
        accelerator_type = "gpu"

    if (accelerator_type == "gpu") and (not torch.cuda.is_available()):
        logger.warning(f"GPUs selected, but will be ignored since no GPU are available on this device")
        accelerator_type = "cpu"

    # Get the IPU info
    ipus = config_acc["config_override"].get("trainer", {}).get("trainer", {}).get("ipus", 0)
    if ipus > 0:
        assert (accelerator_type is None) or (accelerator_type == "ipu"), "Accelerator mismatch"
        accelerator_type = "ipu"
    if accelerator_type == "ipu":
        poptorch = import_poptorch()
        if not poptorch.ipuHardwareIsAvailable():
            logger.warning(f"IPUs selected, but will be ignored since no IPU are available on this device")
            accelerator_type = "cpu"

    # Fall on cpu at the end
    if accelerator_type is None:
        accelerator_type = "cpu"
    return accelerator_type


def _get_ipu_opts(config: Union[omegaconf.DictConfig, Dict[str, Any]]) -> Tuple[str, str]:
    r"""
    Get the paths of the IPU-specific config files from the main YAML config
    """

    accelerator_options = config["accelerator"]
    accelerator_type = accelerator_options["type"]

    if accelerator_type != "ipu":
        return None, None

    ipu_opts = accelerator_options["ipu_config"]
    ipu_inference_opts = accelerator_options.get("ipu_inference_config", None)

    return ipu_opts, ipu_inference_opts


def load_datamodule(
    config: Union[omegaconf.DictConfig, Dict[str, Any]], accelerator_type: str
) -> BaseDataModule:
    """
    Load the datamodule from the specified configurations at the key
    `datamodule: args`.
    If the accelerator is IPU, load the IPU options as well.

    Parameters:
        config: The config file, with key `datamodule: args`
        accelerator_type: The accelerator type, e.g. "cpu", "gpu", "ipu"
    Returns:
        datamodule: The datamodule used to process and load the data
    """

    cfg_data = config["datamodule"]["args"]

    # Instanciate the datamodule
    module_class = DATAMODULE_DICT[config["datamodule"]["module_type"]]

    if accelerator_type != "ipu":
        datamodule = module_class(
            **config["datamodule"]["args"],
        )
        return datamodule

    # IPU specific adjustments
    else:
        ipu_opts, ipu_inference_opts = _get_ipu_opts(config)

        # Default empty values for the IPU configurations
        ipu_training_opts, ipu_inference_opts = None, None

        ipu_dataloader_training_opts = cfg_data.pop("ipu_dataloader_training_opts", {})
        ipu_dataloader_inference_opts = cfg_data.pop("ipu_dataloader_inference_opts", {})
        ipu_training_opts, ipu_inference_opts = load_ipu_options(
            ipu_opts=ipu_opts,
            seed=config["constants"]["seed"],
            model_name=config["constants"]["name"],
            gradient_accumulation=config["trainer"]["trainer"].get("accumulate_grad_batches", None),
            ipu_inference_opts=ipu_inference_opts,
        )
        # Define the Dataloader options for the IPU on the training sets
        bz_train = cfg_data["batch_size_training"]
        ipu_dataloader_training_opts = IPUDataloaderOptions(
            batch_size=bz_train, **ipu_dataloader_training_opts
        )
        ipu_dataloader_training_opts.set_kwargs()

        # Define the Dataloader options for the IPU on the inference sets
        bz_test = cfg_data["batch_size_inference"]
        ipu_dataloader_inference_opts = IPUDataloaderOptions(
            batch_size=bz_test, **ipu_dataloader_inference_opts
        )
        ipu_dataloader_inference_opts.set_kwargs()

        datamodule = module_class(
            ipu_training_opts=ipu_training_opts,
            ipu_inference_opts=ipu_inference_opts,
            ipu_dataloader_training_opts=ipu_dataloader_training_opts,
            ipu_dataloader_inference_opts=ipu_dataloader_inference_opts,
            **config["datamodule"]["args"],
        )

        return datamodule


def load_metrics(config: Union[omegaconf.DictConfig, Dict[str, Any]]) -> Dict[str, MetricWrapper]:
    """
    Loading the metrics to be tracked.
    Parameters:
        config: The config file, with key `metrics`
    Returns:
        metrics: A dictionary of all the metrics
    """

    task_metrics = {}
    cfg_metrics = config.get("metrics", None)
    if cfg_metrics is None:
        return task_metrics
    cfg_metrics = {key: deepcopy(value) for key, value in config["metrics"].items()}
    # Wrap every metric in the class `MetricWrapper` to standardize them
    for task in cfg_metrics:
        task_metrics[task] = {}
        if cfg_metrics[task] is None:
            cfg_metrics[task] = []
        for this_metric in cfg_metrics[task]:
            name = this_metric.pop("name")
            task_metrics[task][name] = MetricWrapper(**this_metric)
    return task_metrics


def load_architecture(
    config: Union[omegaconf.DictConfig, Dict[str, Any]],
    in_dims: Dict[str, int],
) -> Union[FullGraphMultiTaskNetwork, torch.nn.Module]:
    """
    Loading the architecture used for training.
    Parameters:
        config: The config file, with key `architecture`
        in_dims: Dictionary of the input dimensions for various
    Returns:
        architecture: The datamodule used to process and load the data
    """

    if isinstance(config, dict):
        config = omegaconf.OmegaConf.create(config)
    cfg_arch = config["architecture"]

    kwargs = {}

    # Select the architecture
    model_type = cfg_arch["model_type"].lower()
    if model_type == "fullgraphmultitasknetwork":
        model_class = FullGraphMultiTaskNetwork
    else:
        raise ValueError(f"Unsupported model_type=`{model_type}`")

    # Prepare the various kwargs
    pe_encoders_kwargs = (
        dict(cfg_arch["pe_encoders"]) if cfg_arch.get("pe_encoders", None) is not None else None
    )

    pre_nn_kwargs = dict(cfg_arch["pre_nn"]) if cfg_arch["pre_nn"] is not None else None
    pre_nn_edges_kwargs = dict(cfg_arch["pre_nn_edges"]) if cfg_arch["pre_nn_edges"] is not None else None
    gnn_kwargs = dict(cfg_arch["gnn"])
    graph_output_nn_kwargs = (
        dict(cfg_arch["graph_output_nn"]) if cfg_arch["graph_output_nn"] is not None else None
    )
    task_heads_kwargs = (
        cfg_arch["task_heads"] if cfg_arch["task_heads"] is not None else None
    )  # This is of type ListConfig containing TaskHeadParams

    # Initialize the input dimension for the positional encoders
    if pe_encoders_kwargs is not None:
        pe_encoders_kwargs = dict(pe_encoders_kwargs)
        for encoder in pe_encoders_kwargs["encoders"]:
            pe_encoders_kwargs["encoders"][encoder] = dict(pe_encoders_kwargs["encoders"][encoder])
        pe_encoders_kwargs.setdefault(
            "in_dims", in_dims
        )  # set the input dimensions of all pe with info from the data-module
    pe_out_dim = 0 if pe_encoders_kwargs is None else pe_encoders_kwargs.get("out_dim", None)
    edge_pe_out_dim = 0 if pe_encoders_kwargs is None else pe_encoders_kwargs.get("edge_out_dim", None)

    # Set the default `node` input dimension for the pre-processing neural net and graph neural net
    in_dim = in_dims["feat"]
    if pe_out_dim is not None:
        in_dim += pe_out_dim
    if pre_nn_kwargs is not None:
        pre_nn_kwargs = dict(pre_nn_kwargs)
        pre_nn_kwargs.setdefault("in_dim", in_dim)
    else:
        gnn_kwargs.setdefault("in_dim", in_dim)

    # Set the default `edge` input dimension for the pre-processing neural net and graph neural net
    edge_in_dim = in_dims["edge_feat"]
    if edge_pe_out_dim is not None:
        edge_in_dim += edge_pe_out_dim
    if pre_nn_edges_kwargs is not None:
        pre_nn_edges_kwargs = dict(pre_nn_edges_kwargs)
        pre_nn_edges_kwargs.setdefault("in_dim", edge_in_dim)
    else:
        gnn_kwargs.setdefault("in_dim", edge_in_dim)

    # Set the parameters for the full network
    task_heads_kwargs = omegaconf.OmegaConf.to_object(task_heads_kwargs)

    # Set all the input arguments for the model
    model_kwargs = dict(
        gnn_kwargs=gnn_kwargs,
        pre_nn_kwargs=pre_nn_kwargs,
        pre_nn_edges_kwargs=pre_nn_edges_kwargs,
        pe_encoders_kwargs=pe_encoders_kwargs,
        graph_output_nn_kwargs=graph_output_nn_kwargs,
        task_heads_kwargs=task_heads_kwargs,
    )

    return model_class, model_kwargs


def load_predictor(
    config: Union[omegaconf.DictConfig, Dict[str, Any]],
    model_class: Type[torch.nn.Module],
    model_kwargs: Dict[str, Any],
    metrics: Dict[str, MetricWrapper],
    accelerator_type: str,
    task_norms: Optional[Dict[Callable, Any]] = None,
) -> PredictorModule:
    """
    Defining the predictor module, which handles the training logic from `pytorch_lightning.LighningModule`
    Parameters:
        model_class: The torch Module containing the main forward function
        accelerator_type: The accelerator type, e.g. "cpu", "gpu", "ipu"
    Returns:
        predictor: The predictor module
    """

    if accelerator_type == "ipu":
        from graphium.ipu.ipu_wrapper import PredictorModuleIPU

        predictor_class = PredictorModuleIPU
    else:
        predictor_class = PredictorModule

    cfg_pred = dict(deepcopy(config["predictor"]))
    predictor = predictor_class(
        model_class=model_class,
        model_kwargs=model_kwargs,
        metrics=metrics,
        task_norms=task_norms,
        **cfg_pred,
    )

    mup_scale_factor = config["architecture"].pop("mup_scale_factor", None)

    if mup_scale_factor is not None and mup_scale_factor != 1:
        unscaled_model = predictor.model
        scaled_model_kwargs = unscaled_model.scale_kwargs(scale_factor=mup_scale_factor)
        del predictor
        predictor = predictor_class(
            model_class=model_class,
            model_kwargs=scaled_model_kwargs,
            metrics=metrics,
            **cfg_pred,
        )

    # mup base shapes
    mup_base_path = config["architecture"].pop("mup_base_path", None)
    predictor = load_mup(mup_base_path, predictor)

    return predictor


def load_mup(mup_base_path: str, predictor: PredictorModule) -> PredictorModule:
    """
    Load the base shapes for the mup, based either on a `.ckpt` or `.yaml` file.
    If `.yaml`, it should be generated by `mup.save_base_shapes`
    """
    model = predictor.model

    if not isinstance(model, MupMixin):
        raise TypeError("load_mup can only be applied to models that use the MupMixin")

    if mup_base_path is None:
        base = model.__class__(**model.make_mup_base_kwargs(divide_factor=2))
    elif mup_base_path.endswith(".ckpt"):
        base = predictor.__class__.load_from_checkpoint(mup_base_path, map_location="cpu")
    elif mup_base_path.endswith(".yaml"):
        base = mup_base_path
    else:
        raise ValueError(f"Unrecognized file type {mup_base_path}")
    predictor.model = set_base_shapes(predictor.model, base, rescale_params=False)
    return predictor


def load_trainer(
    config: Union[omegaconf.DictConfig, Dict[str, Any]],
    run_name: str,
    accelerator_type: str,
    date_time_suffix: str = "",
) -> Trainer:
    """
    Defining the pytorch-lightning Trainer module.
    Parameters:
        config: The config file, with key `trainer`
        run_name: The name of the current run. To be used for logging.
        accelerator_type: The accelerator type, e.g. "cpu", "gpu", "ipu"
        date_time_suffix: The date and time of the current run. To be used for logging.
    Returns:
        trainer: the trainer module
    """
    cfg_trainer = deepcopy(config["trainer"])

    # Define the IPU plugin if required
    strategy = None
    if accelerator_type == "ipu":
        ipu_opts, ipu_inference_opts = _get_ipu_opts(config)

        training_opts, inference_opts = load_ipu_options(
            ipu_opts=ipu_opts,
            ipu_inference_opts=ipu_inference_opts,
            seed=config["constants"]["seed"],
            model_name=config["constants"]["name"],
            gradient_accumulation=config["trainer"]["trainer"].get("accumulate_grad_batches", None),
        )

        from graphium.ipu.ipu_wrapper import DictIPUStrategy

        strategy = DictIPUStrategy(training_opts=training_opts, inference_opts=inference_opts)

    # Set the number of gpus to 0 if no GPU is available
    _ = cfg_trainer["trainer"].pop("accelerator", None)
    gpus = cfg_trainer["trainer"].pop("gpus", None)
    ipus = cfg_trainer["trainer"].pop("ipus", None)
    if (accelerator_type == "gpu") and (gpus is None):
        gpus = 1
    if (accelerator_type == "ipu") and (ipus is None):
        ipus = 1
    if accelerator_type != "gpu":
        gpus = 0
    if accelerator_type != "ipu":
        ipus = 0

    # Remove the gradient accumulation from IPUs, since it's handled by the device
    if accelerator_type == "ipu":
        cfg_trainer["trainer"].pop("accumulate_grad_batches", None)

    # Define the early stopping parameters
    trainer_kwargs = {}
    callbacks = []
    if "early_stopping" in cfg_trainer.keys():
        callbacks.append(EarlyStopping(**cfg_trainer["early_stopping"]))

    # Define the early model checkpoing parameters
    if "model_checkpoint" in cfg_trainer.keys():
        callbacks.append(ModelCheckpoint(**cfg_trainer["model_checkpoint"]))

    # Define the logger parameters
    logger = cfg_trainer.pop("logger", None)
    if logger is not None:
        name = logger.pop("name", run_name)
        if len(date_time_suffix) > 0:
            name += f"_{date_time_suffix}"
        trainer_kwargs["logger"] = WandbLogger(name=name, **logger)

    trainer_kwargs["callbacks"] = callbacks

    trainer = Trainer(
        detect_anomaly=True,
        strategy=strategy,
        accelerator=accelerator_type,
        ipus=ipus,
        gpus=gpus,
        **cfg_trainer["trainer"],
        **trainer_kwargs,
    )

    return trainer


def save_params_to_wandb(
    logger: Logger,
    config: Union[omegaconf.DictConfig, Dict[str, Any]],
    predictor: PredictorModule,
    datamodule: MultitaskFromSmilesDataModule,
):
    """
    Save a few stuff to weights-and-biases WandB
    Parameters:
        logger: The object used to log the training. Usually WandbLogger
        config: The config file, with key `trainer`
        predictor: The predictor used to handle the train/val/test steps logic
        datamodule: The datamodule used to load the data into training
    """

    # Get the wandb runner and directory
    wandb_run = logger.experiment
    if wandb_run is None:
        wandb_run = ""
    wandb_dir = wandb_run.dir

    # Save the mup base model to WandB as a yaml file
    mup.save_base_shapes(predictor.model, os.path.join(wandb_dir, "mup_base_params.yaml"))

    # Save the full configs as a YAML file
    with open(os.path.join(wandb_dir, "full_configs.yaml"), "w") as file:
        yaml.dump(config, file)

    # Save the featurizer into wandb
    featurizer_path = os.path.join(wandb_dir, "featurizer.pickle")
    joblib.dump(datamodule.smiles_transformer, featurizer_path)

    # Save the featurizer and configs into wandb
    if wandb_run is not None:
        wandb_run.save("*.yaml")
        wandb_run.save("*.pickle")


def load_accelerator(config: Union[omegaconf.DictConfig, Dict[str, Any]]) -> Tuple[Dict[str, Any], str]:
    config = deepcopy(config)
    config_acc = config.get("accelerator", {})

    # Merge the accelerator config with the main config
    config_override = config_acc.get("config_override", {})
    merge_dicts(config, config_override)
    accelerator_type = get_accelerator(config_acc)

    return config, accelerator_type


def merge_dicts(dict_a: Dict[str, Any], dict_b: Dict[str, Any], previous_dict_path: str = "") -> None:
    """
    Recursively merges dict_b into dict_a. If a key is missing from dict_a,
    it is added from dict_b. If a key exists in both, an error is raised.
    `dict_a` is modified in-place.

    Parameters:
        dict_a: The dictionary to merge into. Modified in-place.
        dict_b: The dictionary to merge from.
        previous_dict_path: The key path of the parent dictionary,
        used to track the recursive calls.

    Raises:
        ValueError: If a key path already exists in dict_a.

    """
    for key, value_b in dict_b.items():
        if key not in dict_a:
            dict_a[key] = value_b
        else:
            value_a = dict_a[key]
            if previous_dict_path == "":
                previous_dict_path = key
            else:
                previous_dict_path = f"{previous_dict_path}/{key}"
            if isinstance(value_a, dict) and isinstance(value_b, dict):
                merge_dicts(value_a, value_b, previous_dict_path=previous_dict_path)
            else:
                if value_a != value_b:
                    raise ValueError(f"Dict path already exists: {previous_dict_path}")
