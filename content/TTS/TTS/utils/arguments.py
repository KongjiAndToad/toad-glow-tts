#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Argument parser for training scripts."""

import argparse
import glob
import os
import re

import torch

from TTS.config import load_config
from TTS.tts.utils.text.symbols import parse_symbols
from TTS.utils.console_logger import ConsoleLogger
from TTS.utils.generic_utils import create_experiment_folder, get_git_branch
from TTS.utils.io import copy_model_files
from TTS.utils.tensorboard_logger import TensorboardLogger


def init_arguments(argv):
    """Parse command line arguments of training scripts.

    Args:
        argv (list): This is a list of input arguments as given by sys.argv

    Returns:
        argparse.Namespace: Parsed arguments.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--continue_path",
        type=str,
        help=(
            "Training output folder to continue training. Used to continue "
            "a training. If it is used, 'config_path' is ignored."
        ),
        default="",
        required="--config_path" not in argv,
    )
    parser.add_argument(
        "--restore_path", type=str, help="Model file to be restored. Use to finetune a model.", default=""
    )
    parser.add_argument(
        "--best_path",
        type=str,
        help=(
            "Best model file to be used for extracting best loss."
            "If not specified, the latest best model in continue path is used"
        ),
        default="",
    )
    parser.add_argument(
        "--config_path", type=str, help="Path to config file for training.", required="--continue_path" not in argv
    )
    parser.add_argument("--debug", type=bool, default=False, help="Do not verify commit integrity to run training.")
    parser.add_argument("--rank", type=int, default=0, help="DISTRIBUTED: process rank for distributed training.")
    parser.add_argument("--group_id", type=str, default="", help="DISTRIBUTED: process group id.")

    return parser


def get_last_checkpoint(path):
    """Get latest checkpoint or/and best model in path.

    It is based on globbing for `*.pth.tar` and the RegEx
    `(checkpoint|best_model)_([0-9]+)`.

    Args:
        path (list): Path to files to be compared.

    Raises:
        ValueError: If no checkpoint or best_model files are found.

    Returns:
        last_checkpoint (str): Last checkpoint filename.
    """
    file_names = glob.glob(os.path.join(path, "*.pth.tar"))
    last_models = {}
    last_model_nums = {}
    for key in ["checkpoint", "best_model"]:
        last_model_num = None
        last_model = None
        # pass all the checkpoint files and find
        # the one with the largest model number suffix.
        for file_name in file_names:
            match = re.search(f"{key}_([0-9]+)", file_name)
            if match is not None:
                model_num = int(match.groups()[0])
                if last_model_num is None or model_num > last_model_num:
                    last_model_num = model_num
                    last_model = file_name

        # if there is not checkpoint found above
        # find the checkpoint with the latest
        # modification date.
        key_file_names = [fn for fn in file_names if key in fn]
        if last_model is None and len(key_file_names) > 0:
            last_model = max(key_file_names, key=os.path.getctime)
            last_model_num = torch.load(last_model)["step"]

        if last_model is not None:
            last_models[key] = last_model
            last_model_nums[key] = last_model_num

    # check what models were found
    if not last_models:
        raise ValueError(f"No models found in continue path {path}!")
    if "checkpoint" not in last_models:  # no checkpoint just best model
        last_models["checkpoint"] = last_models["best_model"]
    elif "best_model" not in last_models:  # no best model
        # this shouldn't happen, but let's handle it just in case
        last_models["best_model"] = None
    # finally check if last best model is more recent than checkpoint
    elif last_model_nums["best_model"] > last_model_nums["checkpoint"]:
        last_models["checkpoint"] = last_models["best_model"]

    return last_models["checkpoint"], last_models["best_model"]


def process_args(args):
    """Process parsed comand line arguments.

    Args:
        args (argparse.Namespace or dict like): Parsed input arguments.

    Returns:
        c (TTS.utils.io.AttrDict): Config paramaters.
        out_path (str): Path to save models and logging.
        audio_path (str): Path to save generated test audios.
        c_logger (TTS.utils.console_logger.ConsoleLogger): Class that does
            logging to the console.
        tb_logger (TTS.utils.tensorboard.TensorboardLogger): Class that does
            the TensorBoard loggind.
    """
    if isinstance(args, tuple):
        args, coqpit_overrides = args
    if args.continue_path:
        # continue a previous training from its output folder
        experiment_path = args.continue_path
        args.config_path = os.path.join(args.continue_path, "config.json")
        args.restore_path, best_model = get_last_checkpoint(args.continue_path)
        if not args.best_path:
            args.best_path = best_model
    # setup output paths and read configs
    config = load_config(args.config_path)
    # override values from command-line args
    config.parse_known_args(coqpit_overrides, relaxed_parser=True)
    if config.mixed_precision:
        print("   >  Mixed precision mode is ON")
    experiment_path = args.continue_path
    if not experiment_path:
        experiment_path = create_experiment_folder(config.output_path, config.run_name, args.debug)
    audio_path = os.path.join(experiment_path, "test_audios")
    # setup rank 0 process in distributed training
    if args.rank == 0:
        os.makedirs(audio_path, exist_ok=True)
        new_fields = {}
        if args.restore_path:
            new_fields["restore_path"] = args.restore_path
        new_fields["github_branch"] = get_git_branch()
        # if model characters are not set in the config file
        # save the default set to the config file for future
        # compatibility.
        if config.has("characters_config"):
            used_characters = parse_symbols()
            new_fields["characters"] = used_characters
        copy_model_files(config, experiment_path, new_fields)
        os.chmod(audio_path, 0o775)
        os.chmod(experiment_path, 0o775)
        tb_logger = TensorboardLogger(experiment_path, model_name=config.model)
        # write model desc to tensorboard
        tb_logger.tb_add_text("model-config", f"<pre>{config.to_json()}</pre>", 0)
    c_logger = ConsoleLogger()
    return config, experiment_path, audio_path, c_logger, tb_logger


def init_training(argv):
    """Initialization of a training run."""
    parser = init_arguments(argv)
    args = parser.parse_known_args()
    config, OUT_PATH, AUDIO_PATH, c_logger, tb_logger = process_args(args)
    return args[0], config, OUT_PATH, AUDIO_PATH, c_logger, tb_logger
