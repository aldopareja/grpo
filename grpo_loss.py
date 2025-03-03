import os
import torch
torch.set_float32_matmul_precision('high')
from typing import Tuple
from transformers import AutoTokenizer, AutoModelForCausalLM
model = AutoModelForCausalLM.from_pretrained("Qwen/Qwen2.5-Math-7B")
tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-Math-7B")
'''
rclone copy --copy-links ~/.cache/huggingface/hub/models--Qwen--Qwen2.5-Math-7B/snapshots/b101308fe89651ea5ce025f25317fea6fc07e96e/ /new_data/aldo/models/Qwen2.5-Math-7B
'''
import torch.nn as nn

# def _make_grpo_forward(model):
#     def _forward(
#             input_ids=None,
#             attention_mask=None,
#             position_ids=None,
#             past_key_values=None,
#             inputs_embeds=None,
#             use_cache=None,
#             output_attentions=None,
#             output_hidden_states=None,
#             return_dict=None,
#             cache_position=None,
#             _output_indices=None,
#             **kwargs,
#     ):
#         outputs = model.model(
#             input_ids=input_ids,
#             attention_mask=attention_mask,
#             position_ids=position_ids,
#             past_key_values=past_key_values,
#             inputs_embeds=inputs_embeds,
#             use_cache=use_cache,
#             output_attentions=output_attentions,
#             output_hidden_states=output_hidden_states,
#             return_dict=return_dict,
#             cache_position=cache_position,
#             **kwargs,
#         )
#         hidden_states = outputs[0]
#         # logits = model.lm_head(hidden_states[:, _output_indices, :]).contiguous().float()
#         logits = model.lm_head(hidden_states).contiguous().float()
#         return logits
#     model.__original_forward = model.forward
#     model.forward = _forward
#     return model

# see transformers/loss/loss_utils.py:ForCausalLMLoss
# coming from the fact that logprobs equivalent to -CrossEntropyLoss(logits, labels)
# this is a modification that does exactly the same except that there's no reduction
# and we return the per-token log probabilities as -CrossEntropyLoss(logits, labels)
def PerTokenLogProbsFromCE(
    logits, labels, vocab_size: int, num_items_in_batch: int = None, ignore_index: int = -100, **kwargs
):
    """
    Compute per-token log probabilities from a cross-entropy loss.
    returns a tensor of shape (L,) where L is the total number of tokens in the batch.
    the logprob i corresponds to the token at position i+1 in the input_ids.
    the logprob at position -1 is 0, as well as the logprob at position len(sample)-1 of each sample.
    """
    # Upcast to float if we need to compute the loss to avoid potential precision issues
    logits = logits.float()
    labels = labels.to(logits.device)
    # Shift so that tokens < n predict n
    labels = nn.functional.pad(labels, (0, 1), value=ignore_index)
    shift_labels = labels[..., 1:].contiguous()

    # Flatten the tokens
    logits = logits.view(-1, vocab_size)
    shift_labels = shift_labels.view(-1)
    # Enable model parallelism
    shift_labels = shift_labels.to(logits.device)
    per_token_ce = nn.functional.cross_entropy(logits, shift_labels, reduction="none", ignore_index=ignore_index)
    logprobs = -per_token_ce
    return logprobs

# def get_per_token_logps(model, batched_samples_ids, batched_samples_position_ids, batched_samples_output_indices):
#     """
#     Given logits and target token IDs, compute per-token log probabilities
#     using torch.nn.functional.cross_entropy with reduction='none'.

#     Args:
#         logits (torch.Tensor): Tensor of shape [B, L, V] (B=batch size, L=sequence length, V=vocab size).
#         target (torch.Tensor): Tensor of shape [B, L] containing target token IDs.
#         ignore_index (int): The index that should be ignored in the loss computation.

#     Returns:
#         torch.Tensor: Per-token log probabilities of shape [B, L].
#     """
#     # Assume tokens to predict are shifted by one:
#     #   logits: prediction for t+1 tokens -> remove last time step
#     shift_logits = logits[:, :-1, :]             # shape: [B, L-1, V]
#     shift_target = target[:, 1:].contiguous()      # shape: [B, L-1]

#     # Flatten for cross entropy computation.
#     flat_logits = shift_logits.reshape(-1, shift_logits.size(-1))
#     flat_target = shift_target.reshape(-1)

#     # Compute element-wise cross entropy loss (-log probability).
#     losses = F.cross_entropy(
#         flat_logits, flat_target, reduction="none", ignore_index=ignore_index
#     )  # shape: [B*(L-1)]

#     # Reshape back to [B, L-1]
#     losses = losses.view(shift_target.size())

#     # The log probabilities are the negatives of the loss values.
#     logprobs = -losses
#     return logprobs

#     # ALDO: batched_samples_output_indices index the logits
#     # but the logits correspond to the next token probabilities (logit i is the logit for token i+1)
#     # therefore we need to index the output_ids of the indices + 1
#     output_ids = batched_samples_ids[:, batched_samples_output_indices+1].contiguous().to(logits.device)
#     token_logits = logits.gather(dim=-1, index=output_ids.unsqueeze(-1)).squeeze(-1).contiguous()
#     # Compute logsumexp for normalization over the vocab dimension: shape [1, N]
#     # do a for loop to reduce memory peak
#     logsumexp_values = torch.stack([torch.logsumexp(lg, dim=-1) for lg in logits]).contiguous()
#     # Final log probability per token: shape [1, N]
#     output_log_probs = token_logits - logsumexp_values
#     return output_log_probs

def compute_kl_divergence(
    policy_logprobs: torch.Tensor,
    reference_logprobs: torch.Tensor
) -> torch.Tensor:
    """
    Compute KL divergence between policy and reference model using the Schulman approximation.
    KL ≈ π_ref/π_θ - log(π_ref/π_θ) - 1
    """
    ratio = torch.exp(reference_logprobs - policy_logprobs)
    kl_div = ratio - (reference_logprobs - policy_logprobs) - 1
    return kl_div

def get_mean_per_sample_loss(loss, output_lens_broadcasted, num_samples):
    """
    loss is a tensor of shape [1, N] where N is the total number of tokens across all samples.
    output_lens_broadcasted has the length
    """
    return (loss/output_lens_broadcasted).sum()/num_samples

# @torch.compile
def compute_grpo_loss(
    policy_model,
    minibatch,
    kl_coeff: float,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Compute GRPO loss with its components using the PPO-style probability ratio trick.

    # The policy gradient is computed as:
    #
    #     ∇θ J(πθ) = 𝔼₍τ ∼ πθ₎ [ Σₜ₌₀ᵀ ∇θ log πθ(aₜ | sₜ) · Φₜ ]
    #
    # Here, ∇θ denotes the gradient with respect to the policy parameters θ, and the expectation
    # is over trajectories τ sampled from the policy πθ. In our implementation, we then take an
    # average over the number of trajectories in the batch (at the gradient step level).
    # We also divide by the number of tokens (actions) in each trajectory to ensure long and short
    # trajectories contribute to the gradient equally.
    """
    # torch.autograd.set_detect_anomaly(True)
    # print("\033[1;91;40mDEBUG: remove torch.autograd.set_detect_anomaly(True)\033[0m")
    batch_ids = minibatch["batch_ids"]
    batch_position_ids = minibatch["batch_position_ids"]
    output_indices = minibatch["output_indices"]
    reference_logprobs = minibatch["reference_output_logprobs"]
    advantages = minibatch["advantages"]
    output_lens_broadcasted = minibatch["output_lens_broadcasted"]
    labels = minibatch["labels"]


    model_out = policy_model(
        input_ids=batch_ids,
        position_ids=batch_position_ids,
        labels=labels,
        use_cache=False,
    )

    policy_logprobs = model_out.loss

    ##### DEBUG #####
    # minibatch['samples'][0].keys()
    # minibatch['samples'][1]['sample_text']
    # tokenizer = AutoTokenizer.from_pretrained(policy_model.config._name_or_path)
    # print(tokenizer.decode(batch_ids[:,-1000:].squeeze().tolist()))
    # non_zero_logprobs = policy_logprobs[policy_logprobs != 0]
    # torch.abs(non_zero_logprobs).mean()
    # pol_ref = torch.abs(policy_logprobs - reference_logprobs)
    # kl_div[kl_div != 0].mean()
    # torch.distributed.breakpoint()
    # print(tokenizer.decode(batch_ids[:,115:120].squeeze().tolist()))
    # print(policy_logprobs[1164:1500])
    # print(reference_logprobs[1164:1500])
    # policy_logprobs[policy_logprobs != 0].shape
    # reference_logprobs[reference_logprobs != 0].shape
    # diff = torch.abs(policy_logprobs - reference_logprobs)/torch.abs((policy_logprobs+reference_logprobs)/2+1e-10)
    # idx = diff.argsort(descending=True)
    # diff[idx[:10]]
    # i = idx[0]
    # [print(f"reflogprobs: {reference_logprobs[i]}, policylogprobs: {policy_logprobs[i]}, diff: {diff[i]}") for i in idx[:10]]
    # diff[diff!=0].mean()
    # print(tokenizer.decode(batch_ids[:,i-10:i+10].squeeze().tolist()))
    # diff[diff>1e-1].shape
    # (diff>1e-1).nonzero()
    # torch.distributed.breakpoint()

    ##### DEBUG #####
    
    # this is equal to 1 but it keeps the gradient flowing through the policy_logprobs without affecting the value of the pg_loss (it's only the advantages)
    prob_ratio = torch.exp(policy_logprobs - policy_logprobs.detach()) # this is 1
    pg_loss = -(prob_ratio * advantages) # equal to -advantages since we are maximizing the advantages.
    
    # KL penalty term using the improved approximation
    kl_div = compute_kl_divergence(policy_logprobs, reference_logprobs)
    
    # Combined loss
    loss = pg_loss + kl_coeff * kl_div
    loss_metrics = get_mean_per_sample_loss(loss, output_lens_broadcasted, minibatch["num_samples"]).item()
    pg_loss_metrics = get_mean_per_sample_loss(pg_loss, output_lens_broadcasted, minibatch["num_samples"]).item()
    kl_div_metrics = get_mean_per_sample_loss(kl_div, output_lens_broadcasted, minibatch["num_samples"]).item()
    
    loss = (loss/output_lens_broadcasted).sum()
    # torch.distributed.breakpoint()

    return loss, loss_metrics, pg_loss_metrics, kl_div_metrics
