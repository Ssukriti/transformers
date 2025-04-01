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

import torch
import torch.utils.checkpoint
from torch import nn
import torch.nn.functional as F
from transformers import DynamicCache

from ..granitemoeshared.modeling_granitemoeshared import (
    GraniteMoeSharedMLP,
    GraniteMoeSharedModel,
    GraniteMoeSharedForCausalLM,
    GraniteMoeSharedPreTrainedModel
)
from .configuration_granitemoehybrid import GraniteMoeHybridConfig
from ...utils import add_start_docstrings

class GraniteMultiHeadLatentAttention(nn.Module):
    def __init__(self, config: GraniteMoeHybridConfig):
        super(GraniteMultiHeadLatentAttention, self).__init__()

        self.causal = True
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        # Do we need this or is it always False?
        #self.add_bias = config.add_bias
        self.query_compression_size = config.mla_query_comp_size
        self.key_value_compression_size = config.mla_key_value_comp_size

        self.head_dim = self.hidden_size // self.num_heads 
        # self.position_embedding_type = config.position_embedding_type
        self.attention_multiplier = config.attention_multiplier
        # TODO: where does it come from?
        self.layer_idx = config.layer_idx

        # will bias be a flag in config?
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
        # TO DO the softmax_dropout
        self.softmax_dropout_p = config.softmax_dropout
        self.softmax_dropout = nn.Identity() if config.softmax_dropout == 0 else nn.Dropout(config.softmax_dropout)
        self.dropout = nn.Identity() if config.dropout == 0 else nn.Dropout(config.dropout)
    
    def forward(self,  hidden_states: torch.Tensor,
        past_key_values: DynamicCache | None = None,
        attention_mask: torch.Tensor | None = None,   
    ) -> torch.Tensor:
        
        hidden_states = self.c_attn_down_projection(hidden_states)
        query, key, value = hidden_states.split(
                (self.query_compression_size, self.key_value_compression_size, self.key_value_compression_size), dim=-1
            )
        if past_key_values is not None:
                key, value = past_key_values.update(key, value, self.layer_idx)

        query = self.query_up_projection(query)
        key = self.key_up_projection(key)
        value = self.value_up_projection(value)

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
