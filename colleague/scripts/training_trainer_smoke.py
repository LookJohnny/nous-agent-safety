#!/usr/bin/env python3
"""Offline, synthetic-dataset trainer acceptance. No pretrained weights or APIs.

Run with Python -I. Dependencies must already be installed in an isolated venv.
This script makes no claim about learned model quality or platform wire formats.
"""
import sys
if __name__ == "__main__" and not sys.flags.isolated:
    sys.stderr.write("nous-trainer: isolated_startup_required; use python -I\n")
    raise SystemExit(2)

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import socket


class SmokeError(ValueError):
    pass


def _require(condition, code):
    if not condition:
        raise SmokeError(code)


def _deny_network(*args, **kwargs):
    raise OSError("trainer_network_disabled")


def _write_report(path, report):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")


def _weight_digest(model):
    digest = hashlib.sha256()
    for name, parameter in model.named_parameters():
        digest.update(name.encode())
        digest.update(parameter.detach().cpu().contiguous().numpy().tobytes())
    return digest.hexdigest()


def _validator_for_profile(profile):
    if profile == "codemode-events-v1":
        from nous_codemode_training import validate_sample
    elif profile == "request-sft-v1":
        from nous_training_request import validate_sample
    elif profile == "request-episode-sft-v1":
        from nous_training_episode import validate_sample
    else:
        raise SmokeError("dataset_profile_mismatch")
    return validate_sample


def run(dataset, out, *, steps=2, max_tokens=4096):
    dataset, out = Path(dataset).resolve(), Path(out).resolve()
    _require(steps in (1, 2), "steps_must_be_one_or_two")
    _require(256 <= max_tokens <= 16384, "max_tokens_out_of_range")
    out.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.umask(0o077)
    report = {"schema": "nous.trainer-smoke/1", "status": "failed",
              "scope": "data_loading_and_bounded_optimization_only", "profile": None,
              "pretrained_weights": False, "network_allowed": False, "model_quality_evaluated": False,
              "device": "cpu", "steps_requested": steps, "python": sys.version.split()[0]}
    try:
        os.environ.update(HF_HUB_OFFLINE="1", HF_DATASETS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                          DO_NOT_TRACK="1", TOKENIZERS_PARALLELISM="false", WANDB_DISABLED="true",
                          HF_HOME=str(out / "cache"), HF_DATASETS_CACHE=str(out / "cache/datasets"))
        socket.create_connection = _deny_network
        socket.socket.connect = _deny_network
        socket.socket.connect_ex = _deny_network
        packages = ("torch", "transformers", "tokenizers", "accelerate", "datasets", "trl", "numpy", "pyarrow")
        try:
            report["versions"] = {name: importlib.metadata.version(name) for name in packages}
        except importlib.metadata.PackageNotFoundError:
            raise SmokeError("trainer_dependency_missing") from None
        # Only a trusted checkout's hook directory is added, after isolated startup.
        # This is not a signature verifier or a loader for untrusted Python code.
        hooks = Path(__file__).resolve().parents[1] / "hooks"
        sys.path.insert(0, str(hooks))
        from nous_training_validate import validate_dataset
        validation = validate_dataset(dataset)
        _require(validation.get("valid") is True, "dataset_validation_failed")
        manifest = json.loads((dataset / "manifest.json").read_text())
        validate_sample = _validator_for_profile(manifest.get("profile"))
        report["profile"] = manifest["profile"]
        _require(manifest.get("synthetic") is True, "synthetic_dataset_required")
        row_count = manifest.get("files", {}).get("sft.jsonl", {}).get("records")
        _require(type(row_count) is int and 0 < row_count <= 64, "smoke_sample_count_out_of_range")
        report["dataset_manifest_sha256"] = hashlib.sha256((dataset / "manifest.json").read_bytes()).hexdigest()

        import torch
        from datasets import Features, Value, load_dataset
        from tokenizers import Tokenizer, decoders, models, pre_tokenizers
        from transformers import GPT2Config, GPT2LMHeadModel, PreTrainedTokenizerFast, TrainerCallback, set_seed
        from trl import SFTConfig, SFTTrainer

        torch.set_num_threads(1)
        set_seed(17)
        data = load_dataset("json", data_files=str(dataset / "sft.jsonl"), split="train",
                            features=Features({"prompt": Value("string"), "completion": Value("string")}),
                            cache_dir=str(out / "cache/datasets"))
        _require(0 < len(data) <= 64, "smoke_sample_count_out_of_range")
        for row in data:
            validate_sample(row)
        # Fixed byte alphabet, no merges/normalization: spaces and JS punctuation
        # survive round trips, including the prompt/completion token boundary.
        alphabet = sorted(pre_tokenizers.ByteLevel.alphabet())
        vocab = {token: index for index, token in enumerate(["[PAD]", "[EOS]", "[UNK]", *alphabet])}
        backend = Tokenizer(models.BPE(vocab=vocab, merges=[], unk_token="[UNK]"))
        backend.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False, use_regex=False)
        backend.decoder = decoders.ByteLevel()
        tokenizer = PreTrainedTokenizerFast(tokenizer_object=backend, pad_token="[PAD]", eos_token="[EOS]",
                                           unk_token="[UNK]", model_max_length=max_tokens,
                                           clean_up_tokenization_spaces=False)
        expected = []
        for row in data:
            joined = row["prompt"] + row["completion"]
            _require(not any(token in joined for token in tokenizer.all_special_tokens), "reserved_token_in_fixture")
            ids = tokenizer.encode(joined, add_special_tokens=False)
            _require(tokenizer.decode(ids, clean_up_tokenization_spaces=False) == joined, "tokenizer_roundtrip_failed")
            prefix = tokenizer.encode(row["prompt"], add_special_tokens=False)
            suffix = tokenizer.encode(row["completion"], add_special_tokens=False)
            _require(ids == prefix + suffix, "tokenizer_boundary_changed")
            ids += [tokenizer.eos_token_id]
            _require(len(ids) <= max_tokens, "sample_would_truncate")
            _require(len(suffix) > 0, "empty_completion_tokens")
            expected.append((ids, len(prefix)))
        config = GPT2Config(vocab_size=len(tokenizer), n_layer=2, n_head=2, n_embd=64,
                            n_positions=max_tokens, n_ctx=max_tokens, bos_token_id=tokenizer.eos_token_id,
                            eos_token_id=tokenizer.eos_token_id, pad_token_id=tokenizer.pad_token_id,
                            resid_pdrop=0.0, embd_pdrop=0.0, attn_pdrop=0.0, use_cache=False)
        model = GPT2LMHeadModel(config)
        model.config._attn_implementation = "eager"
        before = _weight_digest(model)
        gradient_checks = []

        class CheckGradients(TrainerCallback):
            def on_pre_optimizer_step(self, args, state, control, model=None, **kwargs):
                grads = [p.grad for p in model.parameters() if p.grad is not None]
                _require(bool(grads), "gradients_missing")
                _require(all(bool(torch.isfinite(g).all()) for g in grads), "nonfinite_gradients")
                norm = math.sqrt(sum(float(g.float().pow(2).sum()) for g in grads))
                _require(math.isfinite(norm) and norm > 0, "zero_or_nonfinite_gradient_norm")
                gradient_checks.append(norm)

        args = SFTConfig(output_dir=str(out / "trainer"), use_cpu=True, bf16=False, fp16=False,
                         per_device_train_batch_size=1, gradient_accumulation_steps=1, max_steps=steps,
                         learning_rate=1e-3, max_length=None, packing=False, padding_free=False,
                         completion_only_loss=True, report_to=[], logging_steps=1, save_strategy="no",
                         eval_strategy="no", dataloader_num_workers=0, disable_tqdm=True,
                         optim="adamw_torch", seed=17, data_seed=17, dataset_num_proc=None)
        trainer = SFTTrainer(model=model, args=args, train_dataset=data, processing_class=tokenizer,
                             callbacks=[CheckGradients()])
        prepared = trainer.train_dataset
        _require(len(prepared) == len(expected), "prepared_row_count_changed")
        target_tokens = 0
        for index, (ids, prefix_len) in enumerate(expected):
            row = prepared[index]
            _require(row["input_ids"] == ids, "prepared_tokens_changed_or_truncated")
            mask = [0] * prefix_len + [1] * (len(ids) - prefix_len)
            _require(row.get("completion_mask") == mask, "completion_mask_mismatch")
            batch = trainer.data_collator([row])
            labels = batch["labels"][0].tolist()
            _require(labels == [-100] * prefix_len + ids[prefix_len:], "prompt_or_tool_output_loss_unmasked")
            target_tokens += len(ids) - prefix_len
        # Exercise padding separately, even if the exported examples happen to
        # have equal lengths, without changing the actual training dataset.
        padded = trainer.data_collator([prepared[0], {"input_ids": [4, 5], "completion_mask": [0, 1]}])
        _require(bool((padded["labels"][padded["attention_mask"] == 0] == -100).all()), "padding_loss_unmasked")
        result = trainer.train()
        loss = float(result.training_loss)
        _require(math.isfinite(loss), "nonfinite_training_loss")
        _require(trainer.state.global_step == steps, "optimizer_step_count_mismatch")
        _require(len(gradient_checks) == steps, "gradient_checks_missing")
        after = _weight_digest(model)
        _require(before != after, "weights_unchanged")
        report.update(status="passed", samples=len(data), target_tokens=target_tokens,
                      max_observed_tokens=max(len(ids) for ids, _ in expected), truncation=False,
                      prompt_and_tool_output_loss_masked=True, padding_loss_masked=True,
                      tokenizer_roundtrip=True, completion_boundary_exact=True, global_step=trainer.state.global_step,
                      loss=loss, finite_gradient_steps=len(gradient_checks), weights_changed=True,
                      initial_weights_sha256=before, final_weights_sha256=after,
                      parameters=sum(p.numel() for p in model.parameters()))
    except Exception as error:
        report["error"] = str(error) if isinstance(error, SmokeError) else "trainer_execution_failed"
        report["error_type"] = type(error).__name__
    _write_report(out / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="Validated synthetic codemode-events-v1 or request-sft-v1 export directory")
    parser.add_argument("--out", required=True, help="New output directory; never overwritten")
    parser.add_argument("--steps", type=int, choices=(1, 2), default=2)
    parser.add_argument("--max-tokens", type=int, default=4096)
    args = parser.parse_args()
    try:
        report = run(args.dataset, args.out, steps=args.steps, max_tokens=args.max_tokens)
    except (OSError, ValueError):
        print(json.dumps({"status": "failed", "error": "invalid_smoke_arguments_or_output"}))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
