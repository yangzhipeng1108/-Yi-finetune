#!/usr/bin/env python
# coding=utf-8
import logging
import math
import os
import sys
import random
from dataclasses import dataclass, field
from itertools import chain
import deepspeed
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
import numpy as np
from typing import Dict, Optional, List, Union

import datasets
import evaluate
import torch
from datasets import load_dataset
from peft import (
    LoraConfig,
    PeftModel,
    get_peft_model,
    get_peft_model_state_dict,
    prepare_model_for_int8_training,
    prepare_model_for_kbit_training,
    set_peft_model_state_dict,
)
import transformers
from transformers.trainer_utils import PREFIX_CHECKPOINT_DIR
from transformers import (
    CONFIG_MAPPING,
    MODEL_FOR_CAUSAL_LM_MAPPING,
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    TrainerCallback,
    TrainerState,
    TrainerControl,
    HfArgumentParser,
    Trainer,
    TrainingArguments,
    default_data_collator,
    BitsAndBytesConfig,
    is_torch_tpu_available,
    set_seed,
    get_scheduler,
)
from transformers.testing_utils import CaptureLogger
from transformers.trainer_utils import get_last_checkpoint
from utils.prompter import Prompter
from transformers.utils import check_min_version, send_example_telemetry
from transformers.utils.versions import require_version
from utils.utils import print_rank_0, to_device, save_hf_format, set_random_seed, get_all_reduce_mean, \
    get_optimizer_grouped_parameters, save_zero_three_model,save_zero_three_model_lora

logger = logging.getLogger(__name__)

MODEL_CONFIG_CLASSES = list(MODEL_FOR_CAUSAL_LM_MAPPING.keys())
MODEL_TYPES = tuple(conf.model_type for conf in MODEL_CONFIG_CLASSES)
DEFAULT_PAD_TOKEN = "[PAD]"
DEFAULT_EOS_TOKEN = "</s>"
DEFAULT_BOS_TOKEN = "<s>"
DEFAULT_UNK_TOKEN = "<unk>"


@dataclass
class ModelArguments:
    model_name_or_path: Optional[str] = field(
        default=None,
        metadata={
            "help": (
                "The model checkpoint for weights initialization.Don't set if you want to train a model from scratch."
            )
        },
    )
    model_type: Optional[str] = field(
        default=None,
        metadata={
            "help": "If training from scratch, pass a model type from the list: " + ", ".join(MODEL_TYPES)},
    )
    config_overrides: Optional[str] = field(
        default=None,
        metadata={
            "help": (
                "Override some existing default config settings when a model is trained from scratch. Example: "
                "n_embd=10,resid_pdrop=0.2,scale_attn_weights=false,summary_type=cls_index"
            )
        },
    )
    config_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"}
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={
            "help": "Where do you want to store the pretrained models downloaded from huggingface.co"},
    )
    lora_r: Optional[int] = field(default=16)
    lora_alpha: Optional[int] = field(default=32)
    target_modules: Optional[str] = field(
        default='q_proj,v_proj,k_proj,o_proj,gate_proj,down_proj,up_proj',
        metadata={
            "help": "List of module names or regex expression of the module names to replace with Lora."
            "For example, ['q', 'v'] or '.*decoder.*(SelfAttention|EncDecAttention).*(q|v)$' "
        },
    )
    use_fast_tokenizer: bool = field(
        default=True,
        metadata={
            "help": "Whether to use one of the fast tokenizer (backed by the tokenizers library) or not."},
    )
    load_in_bits: Optional[int] = field(default=8)
    model_revision: str = field(
        default="main",
        metadata={
            "help": "The specific model version to use (can be a branch name, tag name or commit id)."},
    )
    use_auth_token: bool = field(
        default=False,
        metadata={
            "help": (
                "Will use the token generated when running `huggingface-cli login` (necessary to use this script "
                "with private models)."
            )
        },
    )

    torch_dtype: Optional[str] = field(
        default=None,
        metadata={
            "help": (
                "Override the default `torch.dtype` and load the model under this dtype. If `auto` is passed, the "
                "dtype will be automatically derived from the model's weights."
            ),
            "choices": ["auto", "bfloat16", "float16", "float32"],
        },
    )

    def __post_init__(self):
        if self.config_overrides is not None and (self.config_name is not None or self.model_name_or_path is not None):
            raise ValueError(
                "--config_overrides can't be used in combination with --config_name or --model_name_or_path"
            )
        if type(self.target_modules) == str:
            self.target_modules = self.target_modules.split(',')


@dataclass
class DataTrainingArguments:
    train_on_inputs: bool = field(
        default=False, metadata={"help": "Overwrite the cached training and evaluation sets"}
    )
    dataset_name: Optional[str] = field(
        default=None, metadata={"help": "The name of the dataset to use (via the datasets library)."}
    )
    dataset_config_name: Optional[str] = field(
        default=None, metadata={"help": "The configuration name of the dataset to use (via the datasets library)."}
    )
    train_files: Optional[List[str]] = field(
        default=None, metadata={"help": "The input training data file (a text file)."})
    validation_files: Optional[List[str]] = field(
        default=None,
        metadata={
            "help": "An optional input evaluation data file to evaluate the perplexity on (a text file)."},
    )
    max_train_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "For debugging purposes or quicker training, truncate the number of training examples to this "
                "value if set."
            )
        },
    )
    max_eval_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
                "value if set."
            )
        },
    )
    streaming: bool = field(default=False, metadata={
                            "help": "Enable streaming mode"})
    block_size: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "Optional input sequence length after tokenization. "
                "The training dataset will be truncated in block of this size for training. "
                "Default to the model max input length for single sentence inputs (take into account special tokens)."
            )
        },
    )
    overwrite_cache: bool = field(
        default=False, metadata={"help": "Overwrite the cached training and evaluation sets"}
    )
    validation_split_percentage: Optional[int] = field(
        default=5,
        metadata={
            "help": "The percentage of the train set used as validation set in case there's no validation split"
        },
    )
    preprocessing_num_workers: Optional[int] = field(
        default=None,
        metadata={"help": "The number of processes to use for the preprocessing."},
    )
    keep_linebreaks: bool = field(
        default=True, metadata={"help": "Whether to keep line breaks when using TXT files or not."}
    )

    def __post_init__(self):
        if self.streaming:
            require_version("datasets>=2.0.0",
                            "The streaming feature requires `datasets>=2.0.0`")

        if self.dataset_name is None and self.train_files is None and self.validation_files is None:
            raise ValueError(
                "Need either a dataset name or a training/validation file.")
        else:
            if self.train_files is not None:
                extension = self.train_files[0].split(".")[-1]
                assert extension in [
                    "csv", "json", "txt"], "`train_file` should be a csv, a json or a txt file."
            if self.validation_files is not None:
                extension = self.validation_files[0].split(".")[-1]
                assert extension in [
                    "csv", "json", "txt"], "`validation_file` should be a csv, a json or a txt file."

def save_model(model,  args):
    # used to save huggingface format, so we can use it for hf.from_pretrained
    model_to_save = model.module if hasattr(model, 'module') else model
    WEIGHTS_NAME = "adapter_model.bin"
    os.makedirs(args.output_dir, exist_ok=True)
    output_model_file = os.path.join(args.output_dir, WEIGHTS_NAME)
    output_state_dict = {}
    for k, v in model_to_save.named_parameters():
        if  "lora"  in k:
            v_p = v.cpu()
            output_state_dict[k] = v_p
    for k, v in model_to_save.named_buffers():
        if  "lora"  in k:
            v_p = v.cpu()
            output_state_dict[k] = v_p

    if args.global_rank == 0:
        torch.save(output_state_dict, output_model_file)


def smart_tokenizer_and_embedding_resize(
    special_tokens_dict: Dict,
    tokenizer: transformers.PreTrainedTokenizer,
    model: transformers.PreTrainedModel,
):
    num_new_tokens = tokenizer.add_special_tokens(special_tokens_dict)
    model.resize_token_embeddings(len(tokenizer))

    if num_new_tokens > 0:
        input_embeddings = model.get_input_embeddings().weight.data
        output_embeddings = model.get_output_embeddings().weight.data

        input_embeddings_avg = input_embeddings[:-
                                                num_new_tokens].mean(dim=0, keepdim=True)
        output_embeddings_avg = output_embeddings[:-
                                                  num_new_tokens].mean(dim=0, keepdim=True)

        input_embeddings[-num_new_tokens:] = input_embeddings_avg
        output_embeddings[-num_new_tokens:] = output_embeddings_avg


def _load_dataset(data_args, training_args, model_args):
    data_files = {}
    dataset_args = {}
    if data_args.train_files is not None:
        data_files["train"] = data_args.train_files
    if data_args.validation_files is not None:
        data_files["validation"] = data_args.validation_files
    extension = (
        data_args.train_files[0].split(".")[-1]
        if data_args.train_files is not None
        else data_args.validation_files.split(".")[-1]
    )
    raw_datasets = load_dataset(
        extension,
        data_files=data_files,
        cache_dir=os.path.join(training_args.output_dir, 'dataset_cache'),
        use_auth_token=True if model_args.use_auth_token else None,
        **dataset_args,
    )
    # If no validation data is there, validation_split_percentage will be used to divide the dataset.
    if "validation" not in raw_datasets.keys():
        raw_datasets["validation"] = load_dataset(
            extension,
            data_files=data_files,
            split=f"train[:{data_args.validation_split_percentage}%]",
            cache_dir=model_args.cache_dir,
            use_auth_token=True if model_args.use_auth_token else None,
            **dataset_args,
        )
        raw_datasets["train"] = load_dataset(
            extension,
            data_files=data_files,
            split=f"train[{data_args.validation_split_percentage}%:]",
            cache_dir=model_args.cache_dir,
            use_auth_token=True if model_args.use_auth_token else None,
            **dataset_args,
        )
    return raw_datasets


def train():
    parser = HfArgumentParser(
        (ModelArguments, DataTrainingArguments, TrainingArguments))
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        model_args, data_args, training_args = parser.parse_json_file(
            json_file=os.path.abspath(sys.argv[1]))
    else:
        model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    send_example_telemetry("run_clm", model_args, data_args)

    torch.cuda.set_device(training_args.local_rank)
    device = torch.device("cuda", training_args.local_rank)
    # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
    # torch.distributed.init_process_group(backend='nccl')
    deepspeed.init_distributed()

    training_args.global_rank = torch.distributed.get_rank()

    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    if training_args.should_log:
        transformers.utils.logging.set_verbosity_info()
    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    datasets.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}"
        + f"distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
    )
    logger.info(f"Training/evaluation parameters {training_args}")
    logger.info("**********判断是否存在检查点**********")
    last_checkpoint = None
    if os.path.isdir(training_args.output_dir) and training_args.do_train and not training_args.overwrite_output_dir:
        last_checkpoint = get_last_checkpoint(training_args.output_dir)
        if last_checkpoint is None and len(os.listdir(training_args.output_dir)) > 0:
            raise ValueError(
                f"Output directory ({training_args.output_dir}) already exists and is not empty. "
                "Use --overwrite_output_dir to overcome."
            )
        elif last_checkpoint is not None and training_args.resume_from_checkpoint is None:
            logger.info(
                f"Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change "
                "the `--output_dir` or add `--overwrite_output_dir` to train from scratch."
            )
    logger.info("**********set seed**********")
    set_seed(training_args.seed)

    logger.info("**********装载数据集**********")
    raw_datasets = _load_dataset(data_args, training_args, model_args)

    logger.info("**********从模型装载config**********")
    config_kwargs = {
        "cache_dir": model_args.cache_dir,
        "revision": model_args.model_revision,
        "use_auth_token": True if model_args.use_auth_token else None,
    }
    if model_args.config_name:
        config = AutoConfig.from_pretrained(
            model_args.config_name, **config_kwargs)
    elif model_args.model_name_or_path:
        config = AutoConfig.from_pretrained(
            model_args.model_name_or_path, **config_kwargs)
    else:
        config = CONFIG_MAPPING[model_args.model_type]()
        logger.warning(
            "You are instantiating a new config instance from scratch.")
        if model_args.config_overrides is not None:
            logger.info(f"Overriding config: {model_args.config_overrides}")
            config.update_from_string(model_args.config_overrides)
            logger.info(f"New config: {config}")

    logger.info("**********装载tokenizer**********")
    tokenizer_kwargs = {
        "cache_dir": model_args.cache_dir,
        "use_fast": model_args.use_fast_tokenizer,
        "revision": model_args.model_revision,
        "use_auth_token": True if model_args.use_auth_token else None,
        "padding_side": 'left'
    }
    if model_args.tokenizer_name:
        tokenizer = AutoTokenizer.from_pretrained(
            model_args.tokenizer_name, **tokenizer_kwargs)
    elif model_args.model_name_or_path:
        tokenizer = AutoTokenizer.from_pretrained(
            model_args.model_name_or_path, **tokenizer_kwargs)
    else:
        raise ValueError(
            "You are instantiating a new tokenizer from scratch. This is not supported by this script."
            "You can do it from another script, save it, and load it from here, using --tokenizer_name."
        )

    logger.info("**********使用Lora方式装载模型**********")
    lora_config = LoraConfig(
        r=model_args.lora_r,
        lora_alpha=model_args.lora_alpha,
        target_modules=model_args.target_modules,
        fan_in_fan_out=False,
        lora_dropout=0.05,
        inference_mode=False,
        bias="none",
        task_type="CAUSAL_LM",
    )

    bnb_config_4bit = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16
    )

    bnb_config_8bit = BitsAndBytesConfig(
        load_in_8bit=True
    )

    if model_args.model_name_or_path:
        model = AutoModelForCausalLM.from_pretrained(
            model_args.model_name_or_path,
            from_tf=bool(".ckpt" in model_args.model_name_or_path),
            config=config,
            cache_dir=model_args.cache_dir,
            revision=model_args.model_revision,
            use_auth_token=True if model_args.use_auth_token else None,
            torch_dtype=torch.float16,
            load_in_8bit=True if model_args.load_in_bits == 8 else False,
            quantization_config=bnb_config_4bit if model_args.load_in_bits == 4 else bnb_config_8bit,
            device_map={"": int(os.environ.get("LOCAL_RANK") or 0)}
        )  # .half().cuda()
    else:
        model = AutoModelForCausalLM.from_config(config)
        n_params = sum({p.data_ptr(): p.numel()
                       for p in model.parameters()}.values())
        logger.info(
            f"Training new model from scratch - Total size={n_params/2**20:.2f}M params")

    logger.info("**********调整嵌入的大小**********")
    # We resize the embeddings only when necessary to avoid index errors. If you are creating a model from scratch
    # on a small lora_configvocab and want a smaller embedding size, remove this test.
    embedding_size = model.get_input_embeddings().weight.shape[0]
    if len(tokenizer) > embedding_size:
        model.resize_token_embeddings(len(tokenizer))
    if model_args.load_in_bits == 8:
        model = prepare_model_for_int8_training(model)
    elif model_args.load_in_bits == 4:
        model = prepare_model_for_kbit_training(model)

    train_on_inputs = True
    print('train_on_inputs', train_on_inputs)
    logger.info("**********生成和词元化prompt**********")
    tokenizer.pad_token_id = (
        0  # unk. we want this to be different from the eos token
    )
    tokenizer.padding_side = "left"  # Allow batched inference
    if data_args.block_size is None:
        cutoff_len = 512
    else:
        cutoff_len = data_args.block_size
    prompter = Prompter("alpaca")

    def tokenize(prompt, add_eos_token=True):
        result = tokenizer(
            prompt,
            truncation=True,
            max_length=cutoff_len,
            padding=False,
            return_tensors=None,
        )
        if (
            result["input_ids"][-1] != tokenizer.eos_token_id
            and len(result["input_ids"]) < cutoff_len
            and add_eos_token
        ):
            result["input_ids"].append(tokenizer.eos_token_id)
            result["attention_mask"].append(1)

        result["labels"] = result["input_ids"].copy()

        return result

    def generate_and_tokenize_prompt(data_point):
        full_prompt = prompter.generate_prompt(
            data_point["instruction"],
            data_point["input"],
            data_point["output"],
        )
        tokenized_full_prompt = tokenize(full_prompt)
        if not train_on_inputs:
            user_prompt = prompter.generate_prompt(
                data_point["instruction"], data_point["input"]
            )
            tokenized_user_prompt = tokenize(
                user_prompt, add_eos_token=add_eos_token
            )
            user_prompt_len = len(tokenized_user_prompt["input_ids"])

            if add_eos_token:
                user_prompt_len -= 1

            tokenized_full_prompt["labels"] = [
                -100
            ] * user_prompt_len + tokenized_full_prompt["labels"][
                user_prompt_len:
            ]  # could be sped up, probably
        return tokenized_full_prompt

    tokenized_datasets = raw_datasets.map(generate_and_tokenize_prompt)
    logger.info("**********校验数据集**********")
    if data_args.block_size is None:
        block_size = tokenizer.model_max_length
        if block_size > 2048:
            block_size = 2048
    else:
        block_size = min(data_args.block_size, tokenizer.model_max_length)

    if training_args.do_train:
        if "train" not in tokenized_datasets:
            raise ValueError("--do_train requires a train dataset")
        train_dataset = tokenized_datasets["train"]
        if data_args.max_train_samples is not None:
            max_train_samples = min(
                len(train_dataset), data_args.max_train_samples)
            train_dataset = train_dataset.select(range(max_train_samples))
        for index in random.sample(range(len(train_dataset)), 3):
            logger.info(
                f"Sample {index} of the training set: {train_dataset[index]}.")
        train_dataset = train_dataset.shuffle(seed=training_args.seed)

    if training_args.do_eval:
        if "validation" not in tokenized_datasets:
            raise ValueError("--do_eval requires a validation dataset")
        eval_dataset = tokenized_datasets["validation"]
        if data_args.max_eval_samples is not None:
            max_eval_samples = min(
                len(eval_dataset), data_args.max_eval_samples)
            eval_dataset = eval_dataset.select(range(max_eval_samples))

        def preprocess_logits_for_metrics(logits, labels):
            if isinstance(logits, tuple):
                # Depending on the model and config, logits may contain extra tensors,
                # like past_key_values, but logits always come first
                logits = logits[0]
            return logits.argmax(dim=-1)

        metric = evaluate.load("accuracy.py")

        def compute_metrics(eval_preds):
            preds, labels = eval_preds
            labels = labels[:, 1:].reshape(-1)
            preds = preds[:, :-1].reshape(-1)
            return metric.compute(predictions=preds, references=labels)
        # layer_norm_names=[]
    logger.info("**********peft处理model**********")
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()
    special_tokens_dict = dict()
    if tokenizer.pad_token is None:
        special_tokens_dict["pad_token"] = DEFAULT_PAD_TOKEN
    if tokenizer.eos_token is None:
        special_tokens_dict["eos_token"] = DEFAULT_EOS_TOKEN
    if tokenizer.bos_token is None:
        special_tokens_dict["bos_token"] = DEFAULT_BOS_TOKEN
    if tokenizer.unk_token is None:
        special_tokens_dict["unk_token"] = DEFAULT_UNK_TOKEN
    smart_tokenizer_and_embedding_resize(
        special_tokens_dict=special_tokens_dict,
        tokenizer=tokenizer,
        model=model,
    )
    logger.info("**********初始化训练器**********")
    train_sampler = DistributedSampler(train_dataset)

    eval_sampler = DistributedSampler(eval_dataset)

    data_collator = transformers.DataCollatorWithPadding(
        tokenizer
    )
    eval_dataloader = DataLoader(eval_dataset,
                                 collate_fn=default_data_collator,
                                 sampler=eval_sampler,
                                 batch_size=training_args.per_device_eval_batch_size)

    train_dataloader = DataLoader(train_dataset,
                                  collate_fn=default_data_collator,
                                  sampler=train_sampler,
                                  batch_size=training_args.per_device_train_batch_size)

    optimizer_grouped_parameters = get_optimizer_grouped_parameters(
        model, training_args.weight_decay)
    optimizer = torch.optim.Adam(optimizer_grouped_parameters,
                              lr=training_args.learning_rate,
                              betas=(0.9, 0.95))
    num_update_steps_per_epoch = math.ceil(
        len(train_dataset) / training_args.gradient_accumulation_steps)
    lr_scheduler = get_scheduler(
        name=training_args.lr_scheduler_type,
        optimizer=optimizer,
        num_warmup_steps=20,
        num_training_steps=training_args.num_train_epochs * num_update_steps_per_epoch,
    )

    def evaluation(model, eval_dataloader):
        model.eval()
        losses = 0
        metrics = []
        for step, batch in enumerate(eval_dataloader):
            batch = to_device(batch, device)
            with torch.no_grad():
                outputs = model(**batch)

            preds = preprocess_logits_for_metrics(outputs.logits, batch)

            eval_preds = (preds, batch['labels'])
            metric = compute_metrics(eval_preds)
            metrics.append(metric['accuracy'])

        return np.mean(metrics)

    from datetime import datetime
    start_time = datetime.now()

    # with torch.cuda.amp.autocast():
    for epoch in range(int(training_args.num_train_epochs)):
        print_rank_0(
            f"Beginning of Epoch {epoch + 1}/{training_args.num_train_epochs}, Total Micro Batches {len(train_dataloader)}",
            training_args.global_rank)
        model.train()
        for step, batch in enumerate(train_dataloader):
            # logger.info(batch['input_ids'].shape)
            # logger.info(batch['attention_mask'].shape)
            # logger.info(batch['labels'].shape)
            optimizer.zero_grad()
            # logger.info(batch)
            batch = to_device(batch, device)
            outputs = model(**batch, use_cache=False)
            loss = outputs.loss
            loss.backward()
            optimizer.step()
            lr_scheduler.step()

        # Evaluate perplexity on the validation set.
        print_rank_0(
            f"***** Evaluating perplexity, Epoch {epoch + 1}/{training_args.num_train_epochs} *****",
            training_args.global_rank)
        perplexity = evaluation(model, eval_dataloader)
        print_rank_0(f"ppl: {perplexity}", training_args.global_rank)
        # model.tput_timer.update_epoch_count()
    time = datetime.now() -start_time
    print_rank_0(f'waste time:  {time}', training_args.global_rank)

    if training_args.output_dir is not None:
        print_rank_0('saving the final model ...', training_args.global_rank)

        if training_args.global_rank == 0:
            save_model(model, training_args)


if __name__ == "__main__":
    # with torch.autocast("cuda"):
    train()


'''
UserWarning: MatMul8bitLt: inputs will be cast from torch.float32 to float16 during quantization
  warnings.warn(f"MatMul8bitLt: inputs will be cast from {A.dtype} to float16 during quantization")
'''