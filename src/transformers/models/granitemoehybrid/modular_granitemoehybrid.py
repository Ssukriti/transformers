# coding=utf-8
# Copyright 2025 IBM and the HuggingFace Inc. team. All rights reserved.
#
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from typing import List, Optional, Tuple

import torch
import torch.utils.checkpoint
from torch import nn
import torch.nn.functional as F

from ...cache_utils import Cache, DynamicCache
from ..bamba.configuration_bamba import BambaConfig
from ..bamba.modeling_bamba import BambaMixer
from ..granitemoeshared.modeling_granitemoeshared import (
    GraniteMoeSharedDecoderLayer,
    GraniteMoeSharedMLP,
    GraniteMoeSharedModel,
    GraniteMoeSharedForCausalLM,
    GraniteMoeSharedPreTrainedModel
)
from .configuration_granitemoehybrid import GraniteMoeHybridConfig
from ...utils import add_start_docstrings

class GraniteMultiHeadLatentAttention(nn.Module):
    def __init__(self, config: GraniteMoeHybridConfig, layer_idx: int):
        super(GraniteMultiHeadLatentAttention, self).__init__()

        self.causal = True
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        # TO DO add this bias later
        #self.add_bias = config.add_bias
        self.query_compression_size = config.mla_query_comp_size
        self.key_value_compression_size = config.mla_key_value_comp_size

        self.head_dim = self.hidden_size // self.num_heads 
        # self.position_embedding_type = config.position_embedding_type
        self.attention_multiplier = config.attention_multiplier
        self.layer_idx = layer_idx

        # TO DO- will bias be a flag in config?
        # self.position_embedding_type == "rope": not implemented so went to else
        self.c_attn_down_projection = nn.Linear(self.hidden_size, self.query_compression_size + 2 * self.key_value_compression_size, bias=False)
        self.query_up_projection = nn.Linear(
                self.query_compression_size, self.hidden_size, bias=False
            )
        self.key_up_projection = nn.Linear(
                self.key_value_compression_size, self.hidden_size, bias=False
            )
        self.value_up_projection = nn.Linear(
                self.key_value_compression_size, self.hidden_size, bias=False
            )
        self.c_proj = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        # TO confirm the softmax_dropout and dropout variable names
        self.softmax_dropout_p = config.mla_softmax_dropout
        self.softmax_dropout = nn.Identity() if config.mla_softmax_dropout == 0 else nn.Dropout(config.mla_softmax_dropout)
        self.dropout = nn.Identity() if config.mla_dropout == 0 else nn.Dropout(config.mla_dropout)
    
    def forward(self,  hidden_states: torch.Tensor,
        past_key_values: DynamicCache | None = None,
        attention_mask: torch.Tensor | None = None,   
    ) -> torch.Tensor:
        
        hidden_states = self.c_attn_down_projection(hidden_states)
        # if self.position_embedding_type == "rope":
        #     raise NotImplementedError()
        query, key, value = hidden_states.split(
                (self.query_compression_size, self.key_value_compression_size, self.key_value_compression_size), dim=-1
            )
        if past_key_values is not None:
                key, value = past_key_values.update(key, value, self.layer_idx)

        query = self.query_up_projection(query)
        key = self.key_up_projection(key)
        value = self.value_up_projection(value)
        # reference 
        # https://github.com/IBM/dolomite-engine/blob/main/dolomite_engine/hf_models/modeling_utils/sequence_mixer_blocks/multihead_latent_attention.py#L177
        batch_size, query_length = query.shape[:-1]
        key_length = key.shape[1]

        query = query.view(batch_size, query_length, self.num_heads, -1).transpose(1, 2)
        key = key.view(batch_size, key_length, self.num_heads, -1).transpose(1, 2)
        value = value.view(batch_size, key_length, self.num_heads, -1).transpose(1, 2)

        hidden_states = F.scaled_dot_product_attention(
            query,
            key,
            value,
            attn_mask=attention_mask,
            dropout_p=self.softmax_dropout_p if self.training else 0,
            is_causal=self.causal if attention_mask is None else False,
            scale=self._get_softmax_scale(),
        )

        del query, key, value

        batch_size = hidden_states.shape[0]
        hidden_states = hidden_states.transpose(1, 2)
        hidden_states = hidden_states.reshape(batch_size, -1, self.num_heads * self.head_dim)

        hidden_states = self.c_proj(hidden_states)
        hidden_states = self.dropout(hidden_states)

        return hidden_states

        
    def _get_softmax_scale(self) -> float:
        if self.attention_multiplier is None:
            softmax_scale = None
        else:
            softmax_scale = self.attention_multiplier

        return softmax_scale   
    
class GraniteMoeHybridMambaLayer(BambaMixer):
     def __init__(self, config: GraniteMoeHybridConfig, layer_idx: int):
        # TO DO map variables here
        super().__init__(
            BambaConfig(config),
            layer_idx
        )

class GraniteMoeSharedMLP(GraniteMoeSharedMLP):
    def __init__(self, config: GraniteMoeHybridConfig):
        super().__init__(config)
        
class GraniteMoeHybridDecoderLayer(GraniteMoeSharedDecoderLayer):
    def __init__(self, config: GraniteMoeHybridConfig, layer_idx: int):
        super().__init__(config, layer_idx)
        self.shared_mlp = None if config.shared_intermediate_size == 0 else GraniteMoeSharedMLP(config)
        # to do later on add error handling for not found here 
        self.self_attn = GraniteMultiHeadLatentAttention(config, layer_idx) if config.layer_types[layer_idx] == "multihead_latent_attention" else GraniteMoeHybridMambaLayer(config, layer_idx)

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Cache] = None,
        output_attentions: Optional[bool] = False,
        use_cache: Optional[bool] = False,
        cache_position: Optional[torch.LongTensor] = None,
        output_router_logits: Optional[bool] = False,
        position_embeddings: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,  # necessary, but kept here for BC
        **kwargs,
    ) -> Tuple[torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]]:
        """
        Args:
            hidden_states (`torch.FloatTensor`): input to the layer of shape `(batch, seq_len, embed_dim)`
            attention_mask (`torch.FloatTensor`, *optional*):
                attention mask of size `(batch_size, sequence_length)` if flash attention is used or `(batch_size, 1,
                query_sequence_length, key_sequence_length)` if default attention is used.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers. See `attentions` under
                returned tensors for more detail.
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
            past_key_value (`Tuple(torch.FloatTensor)`, *optional*): cached past key and value projection states
            cache_position (`torch.LongTensor` of shape `(sequence_length)`, *optional*):
                Indices depicting the position of the input sequence tokens in the sequence
            output_router_logits (`bool`, *optional*):
                Whether or not to return the logits of all the routers. They are useful for computing the router loss, and
                should not be returned during inference.
            position_embeddings (`Tuple[torch.FloatTensor, torch.FloatTensor]`, *optional*):
                Tuple containing the cosine and sine positional embeddings of shape `(batch_size, seq_len, head_dim)`,
                with `head_dim` being the embedding dimension of each attention head.
            kwargs (`dict`, *optional*):
                Arbitrary kwargs to be ignored, used for FSDP and other methods that injects code
                into the model
        """
        residual = hidden_states

        hidden_states = self.input_layernorm(hidden_states)

        # check implementation of this function
        hidden_states, self_attn_weights, present_key_value = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_value=past_key_value,
            output_attentions=output_attentions,
            use_cache=use_cache,
            cache_position=cache_position,
            position_embeddings=position_embeddings,
            **kwargs,
        )

        hidden_states = residual + hidden_states * self.residual_multiplier

        # Fully Connected
        residual = hidden_states
        hidden_states = self.post_attention_layernorm(hidden_states)
        # confirm moe is needed. 
        # referral https://github.com/IBM/dolomite-engine/blob/main/dolomite_engine/hf_models/models/gpt_dolomite/layer.py
        # dolomite does not have moe block. This is taken from sharedmoe class however
        moe_hidden_states, router_logits = self.block_sparse_moe(hidden_states)

        if self.shared_mlp is None:
            hidden_states = moe_hidden_states
        else:
            hidden_states = moe_hidden_states + self.shared_mlp(hidden_states)

        del moe_hidden_states

        hidden_states = residual + hidden_states * self.residual_multiplier

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (self_attn_weights,)

        if use_cache:
            outputs += (present_key_value,)

        if output_router_logits:
            outputs += (router_logits,)

        return outputs
    
# TO DO update docstring
GRANITEMOEHYBRID_START_DOCSTRING = r"""
    This model inherits from [`PreTrainedModel`]. Check the superclass documentation for the generic methods the
    library implements for all its model (such as downloading or saving, resizing the input embeddings, pruning heads
    etc.)

    This model is also a PyTorch [torch.nn.Module](https://pytorch.org/docs/stable/nn.html#torch.nn.Module) subclass.
    Use it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to general usage
    and behavior.

    Parameters:
        config ([`GraniteMoeHybridConfig`]):
            Model configuration class with all the parameters of the model. Initializing with a config file does not
            load the weights associated with the model, only the configuration. Check out the
            [`~PreTrainedModel.from_pretrained`] method to load the model weights.
"""


@add_start_docstrings(
    "The bare GraniteMoeHybrid Model outputting raw hidden-states without any specific head on top.",
    GRANITEMOEHYBRID_START_DOCSTRING,
)
class GraniteMoeHybridPreTrainedModel(GraniteMoeSharedPreTrainedModel):
    config_class = GraniteMoeHybridConfig
    _no_split_modules = ["GraniteMoeHybridDecoderLayer"]


GRANITEMOEHYBRID_INPUTS_DOCSTRING = r"""
    Args:
        input_ids (`torch.LongTensor` of shape `(batch_size, sequence_length)`):
            Indices of input sequence tokens in the vocabulary. Padding will be ignored by default should you provide
            it.

            Indices can be obtained using [`AutoTokenizer`]. See [`PreTrainedTokenizer.encode`] and
            [`PreTrainedTokenizer.__call__`] for details.

            [What are input IDs?](../glossary#input-ids)
        attention_mask (`torch.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
            Mask to avoid performing attention on padding token indices. Mask values selected in `[0, 1]`:

            - 1 for tokens that are **not masked**,
            - 0 for tokens that are **masked**.

            [What are attention masks?](../glossary#attention-mask)

            Indices can be obtained using [`AutoTokenizer`]. See [`PreTrainedTokenizer.encode`] and
            [`PreTrainedTokenizer.__call__`] for details.

            If `past_key_values` is used, optionally only the last `input_ids` have to be input (see
            `past_key_values`).

            If you want to change padding behavior, you should read [`modeling_opt._prepare_decoder_attention_mask`]
            and modify to your needs. See diagram 1 in [the paper](https://arxiv.org/abs/1910.13461) for more
            information on the default strategy.

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.
        position_ids (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
            Indices of positions of each input sequence tokens in the position embeddings. Selected in the range `[0,
            config.n_positions - 1]`.

            [What are position IDs?](../glossary#position-ids)
        past_key_values (`Cache` or `tuple(tuple(torch.FloatTensor))`, *optional*):
            Pre-computed hidden-states (key and values in the self-attention blocks and in the cross-attention
            blocks) that can be used to speed up sequential decoding. This typically consists in the `past_key_values`
            returned by the model at a previous stage of decoding, when `use_cache=True` or `config.use_cache=True`.

            Two formats are allowed:
            - a [`~cache_utils.Cache`] instance;
            - Tuple of `tuple(torch.FloatTensor)` of length `config.n_layers`, with each tuple having 2 tensors of
            shape `(batch_size, num_heads, sequence_length, embed_size_per_head)`). This is also known as the legacy
            cache format.

            The model will output the same cache format that is fed as input. If no `past_key_values` are passed, the
            legacy cache format will be returned.

            If `past_key_values` are used, the user can optionally input only the last `input_ids` (those that don't
            have their past key value states given to this model) of shape `(batch_size, 1)` instead of all `input_ids`
            of shape `(batch_size, sequence_length)`.
        inputs_embeds (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`, *optional*):
            Optionally, instead of passing `input_ids` you can choose to directly pass an embedded representation. This
            is useful if you want more control over how to convert `input_ids` indices into associated vectors than the
            model's internal embedding lookup matrix.
        use_cache (`bool`, *optional*):
            If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding (see
            `past_key_values`).
        output_attentions (`bool`, *optional*):
            Whether or not to return the attentions tensors of all attention layers. See `attentions` under returned
            tensors for more detail.
        output_hidden_states (`bool`, *optional*):
            Whether or not to return the hidden states of all layers. See `hidden_states` under returned tensors for
            more detail.
        return_dict (`bool`, *optional*):
            Whether or not to return a [`~utils.ModelOutput`] instead of a plain tuple.
        cache_position (`torch.LongTensor` of shape `(sequence_length)`, *optional*):
            Indices depicting the position of the input sequence tokens in the sequence. Contrarily to `position_ids`,
            this tensor is not affected by padding. It is used to update the cache in the correct position and to infer
            the complete sequence length.
"""


@add_start_docstrings(
    "The bare GraniteMoeShared Model outputting raw hidden-states without any specific head on top.",
    GRANITEMOEHYBRID_START_DOCSTRING,
)
class GraniteMoeHybridModel(GraniteMoeSharedModel):
    """
    Transformer decoder consisting of *config.num_hidden_layers* layers. Each layer is a [`GraniteMoeDecoderLayer`]

    Args:
        config: GraniteMoeHybridConfig
    """

    def __init__(self, config: GraniteMoeHybridConfig):
        super().__init__(config)
        self.layers = nn.ModuleList(
            [GraniteMoeHybridDecoderLayer(config, layer_idx) for layer_idx in range(config.num_hidden_layers)]
        )


class GraniteMoeHybridForCausalLM(GraniteMoeSharedForCausalLM):
    _tied_weights_keys = ["lm_head.weight"]

    def __init__(self, config: GraniteMoeHybridConfig):
        super().__init__(config)
        self.model = GraniteMoeHybridModel(config)
        # Initialize weights and apply final processing
        self.post_init()


__all__ = ["GraniteMoeHybridForCausalLM", "GraniteMoeHybridModel", "GraniteMoeHybridPreTrainedModel"]
