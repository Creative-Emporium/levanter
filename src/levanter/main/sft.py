import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Union

import jax.random as jrandom
import transformers

import haliax as hax
from haliax import Axis
from haliax.partitioning import round_axis_for_partitioning

import levanter
from levanter import callbacks
from levanter.compat.hf_checkpoints import HFCheckpointConverter, HFCompatConfig, save_hf_checkpoint_callback
from levanter.data import PermutationDataset
from levanter.data.text import (
    ChatUrlDataSourceConfig,
    EpochDataset,
    SupervisedSourceConfig,
    mk_chat_sft_dataset,
    mk_supervised_dataset,
)
from levanter.models.llama import LlamaConfig
from levanter.models.lm_model import LmConfig, LmHeadModel, compute_next_token_loss
from levanter.optim import AdamConfig, OptimizerConfig
from levanter.trainer import Trainer, TrainerConfig


logger = logging.getLogger(__name__)

# Define default special tokens
DEFAULT_PAD_TOKEN = "[PAD]"
DEFAULT_EOS_TOKEN = "</s>"
DEFAULT_BOS_TOKEN = "<s>"
DEFAULT_UNK_TOKEN = "<unk>"


class DatasetType(str, Enum):
    """Type of dataset to use"""

    HUGGINGFACE = "huggingface"  # Use HF dataset
    CHAT_JSONL = "chat_jsonl"  # Use JSONL files with chat format


@dataclass
class SFTConfig:
    # inherit most of the config from TrainLmConfig
    trainer: TrainerConfig = field(default_factory=TrainerConfig)
    model: LmConfig = field(default_factory=LlamaConfig)
    optimizer: OptimizerConfig = field(default_factory=AdamConfig)
    supervised_data: Optional[SupervisedSourceConfig | dict[str, SupervisedSourceConfig]] = None

    # config related to continued pretraining
    initialize_from_hf: Union[bool, str] = False
    hf_save_path: Optional[str] = None
    hf_upload: Optional[str] = None
    hf_save_steps: int = 0

    max_seq_len: int = 2048
    model_name_or_path: str = "meta-llama/Llama-2-7b-hf"
    tokenizer: str = "meta-llama/Llama-2-7b-hf"

    # Add dataset type and chat-specific fields
    dataset_type: DatasetType = DatasetType.CHAT_JSONL
    chat_train_urls: Optional[List[str]] = None
    messages_field: str = "messages"
    input_role: str = "user"
    output_role: str = "assistant"

    data_seed: Optional[int] = None  # if provided, will override the data seed from the trainer

    # if provided, will initialize from this checkpoint, used for llama style data mixture
    epoch: int = 0


def train(config: SFTConfig):
    tokenizer = transformers.AutoTokenizer.from_pretrained(
        config.tokenizer,
        model_max_length=config.max_seq_len,
        padding_side="right",
        trust_remote_code=True,
    )
    logger.info(f"Loaded tokenizer {tokenizer}")

    if config.initialize_from_hf:
        if config.trainer.initialize_from is not None:
            raise ValueError("Cannot use both --initialize_from_hf and --initialize_from")

        assert isinstance(config.model, HFCompatConfig)

        converter = HFCheckpointConverter.from_hf(config.model_name_or_path, trust_remote_code=True)
        if hasattr(tokenizer, "vocab") and tokenizer.vocab != converter.tokenizer.vocab:
            logger.warning("The tokenizers appear to be different. You may want to check this.")
        if isinstance(config.initialize_from_hf, str):
            converter = converter.replaced(reference_checkpoint=config.initialize_from_hf, tokenizer=tokenizer)
        else:
            converter = converter.replaced(tokenizer=tokenizer)

        model_config = converter.default_config
    elif config.trainer.initialize_from is None:
        raise ValueError("Must specify either --initialize_from_hf or --initialize_from")
    else:
        if config.hf_save_steps:
            converter = HFCheckpointConverter.from_hf(config.model_name_or_path, trust_remote_code=True)
            converter = converter.replaced(tokenizer=tokenizer)
        else:
            converter = None
        model_config = config.model

    levanter.initialize(config)

    num_new_tokens = add_special_tokens(tokenizer)
    logger.info(f"Added {num_new_tokens} new tokens")
    # randomness in jax is tightly controlled by "keys" which are the states of the random number generators
    # this makes deterministic training pretty easy
    seed = config.trainer.seed
    data_key, _, model_key, training_key = jrandom.split(jrandom.PRNGKey(seed), 4)

    if config.data_seed is not None:
        logger.info(f"Overriding data seed with {config.data_seed}")
        data_key = jrandom.PRNGKey(config.data_seed)

    # Create supervised dataset using generic machinery
    logger.info("Creating supervised dataset")
    if config.dataset_type == DatasetType.CHAT_JSONL:
        assert config.chat_train_urls is not None
        assert config.supervised_data is not None

        # Get the cache_dir safely
        cache_dir = (
            config.supervised_data.cache_dir
            if not isinstance(config.supervised_data, dict)
            else next(iter(config.supervised_data.values())).cache_dir
        )

        chat_config = ChatUrlDataSourceConfig(
            cache_dir=cache_dir,
            train_urls=config.chat_train_urls,  # No validation in this config
            messages_field=config.messages_field,
            input_role=config.input_role,
            output_role=config.output_role,
        )
        train_dataset = mk_chat_sft_dataset(chat_config, tokenizer, model_config.Pos)
    else:
        assert config.supervised_data is not None
        if isinstance(config.supervised_data, dict):
            # TODO: figure out what actually makes sense here
            # for marin we will just use the url code path
            config_to_use = next(iter(config.supervised_data.values()))
        else:
            config_to_use = config.supervised_data
        train_dataset = mk_supervised_dataset(config_to_use, "train", tokenizer, model_config.Pos)
    logger.info("Supervised dataset created")
    train_dataset = PermutationDataset(train_dataset, data_key)

    # Then wrap for epochs
    if config.epoch > 0:
        logger.info(f"Wrapping dataset for {config.epoch} epochs")
        train_dataset = EpochDataset(train_dataset, max_epochs=config.epoch)

    logger.info("Creating optimizer")
    optimizer = config.optimizer.build(config.trainer.num_train_steps)

    # Using the trainer as a context manager does 3 things:
    # 1. Sets the device mesh
    # 2. Sets the axis mapping (for fsdp)
    # 3. Sets the global metrics tracker
    with Trainer(config.trainer, optimizer, loss_fn=compute_next_token_loss) as trainer:  # type: ignore
        parameter_axis_mapping = trainer.parameter_axis_mapping

        # We have two axis_mappings: one for storing the model and optimizer states, and one for compute
        # This allows Zero-3-style parameter sharding, where we shard the parameters and optimizer state across the mesh
        parameter_axis_mapping = trainer.parameter_axis_mapping

        # some axes we need
        Pos = config.model.Pos

        # to do partitioning, our dimensions have to be divisible by the size of the physical axes they're mapped to
        # For most things, we just insist you specify the config right, but tokenizers often have strange numbers of
        # tokens: gpt-2 has 50257, for example. So we round up.
        vocab_size = len(tokenizer)
        Vocab = round_axis_for_partitioning(Axis("vocab", vocab_size), parameter_axis_mapping)
        if config.initialize_from_hf:
            logger.info(f"Loading pretrained model from {converter.reference_checkpoint}")
            model: LmHeadModel = converter.load_pretrained(
                model_config.model_type, axis_mapping=parameter_axis_mapping, dtype=trainer.mp.param_dtype
            )  # type: ignore
            model = hax.named_jit(lambda m: m.resize_vocab(len(tokenizer)))(model)
            state = trainer.initial_state(training_key, model=model)
        else:
            if vocab_size != Vocab.size:
                logger.info(f"Rounding vocab size from {vocab_size} to {Vocab.size} for partitioning")
            state = trainer.initial_state(training_key, model_init=lambda: config.model.build(Vocab, key=model_key))

        flops_per_token = config.model.flops_per_token(vocab_size)
        flops_per_example = 3 * flops_per_token * Pos.size if flops_per_token is not None else None
        trainer.add_hook(
            callbacks.log_performance_stats(Pos.size, trainer.config.train_batch_size, flops_per_example), every=1
        )

        loader = trainer.data_loader(train_dataset, trainer.TrainBatch)

        if config.hf_save_path is not None:
            # bit gross to reach this far into the config, but it's fine
            if config.trainer.checkpointer.append_run_id_to_base_path:
                full_save_path = os.path.join(config.hf_save_path, trainer.run_id)
            else:
                full_save_path = config.hf_save_path

            trainer.add_hook(
                save_hf_checkpoint_callback(full_save_path, converter, upload_to_hf=config.hf_upload or False),
                every=config.hf_save_steps,
            )

        trainer.train(state, loader)


def add_special_tokens(tokenizer, use_unk_instead_of_adding=False):
    special_tokens_dict = dict()
    if use_unk_instead_of_adding:
        if tokenizer.unk_token is None:
            raise ValueError("use_unk_instead_of_add is True but tokenizer doesn't have an unk token")

    unk = tokenizer.unk_token if use_unk_instead_of_adding else None

    if tokenizer.pad_token is None:
        special_tokens_dict["pad_token"] = DEFAULT_PAD_TOKEN if not use_unk_instead_of_adding else unk
    if tokenizer.eos_token is None:
        special_tokens_dict["eos_token"] = DEFAULT_EOS_TOKEN if not use_unk_instead_of_adding else unk
    if tokenizer.bos_token is None:
        special_tokens_dict["bos_token"] = DEFAULT_BOS_TOKEN if not use_unk_instead_of_adding else unk
    if tokenizer.unk_token is None:
        special_tokens_dict["unk_token"] = DEFAULT_UNK_TOKEN

    return tokenizer.add_special_tokens(special_tokens_dict)


if __name__ == "__main__":
    levanter.config.main(train)()