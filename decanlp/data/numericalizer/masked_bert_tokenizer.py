#
# Copyright (c) 2019-2020 The Board of Trustees of the Leland Stanford Junior University
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

# This file was partially copied from huggingface's tokenizers library
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
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

from transformers import BertTokenizer

class MaskedWordPieceTokenizer:
    def __init__(self, vocab, added_tokens_encoder, added_tokens_decoder, unk_token, max_input_chars_per_word=100):
        self.vocab = vocab
        self.unk_token = unk_token
        self.max_input_chars_per_word = max_input_chars_per_word
        self.added_tokens_encoder = added_tokens_encoder
        self.added_tokens_decoder = added_tokens_decoder

    def tokenize(self, tokens, mask):
        output_tokens = []
        for token, should_word_split in tokens, mask:
            if not should_word_split:
                if token not in self.vocab and token not in self.added_tokens_encoder:
                    token_id = len(self.added_tokens_encoder)
                    self.added_tokens_encoder[token] = token_id
                    self.added_tokens_decoder[token_id] = token
                output_tokens.append(token)
                continue

            chars = list(token)
            if len(chars) > self.max_input_chars_per_word:
                output_tokens.append(self.unk_token)
                continue

            is_bad = False
            start = 0
            sub_tokens = []
            while start < len(chars):
                end = len(chars)
                cur_substr = None
                while start < end:
                    substr = "".join(chars[start:end])
                    if start > 0:
                        substr = "##" + substr
                    if substr in self.vocab:
                        cur_substr = substr
                        break
                    end -= 1
                if cur_substr is None:
                    is_bad = True
                    break
                sub_tokens.append(cur_substr)
                start = end

            if is_bad:
                output_tokens.append(self.unk_token)
            else:
                output_tokens.extend(sub_tokens)
        return output_tokens


class MaskedBertTokenizer(BertTokenizer):
    """
    A modified BertTokenizer that respects a mask deciding whether a token should be split or not.
    """
    def __init__(self, *args, do_lower_case, do_basic_tokenize, **kwargs):
        # override do_lower_case and do_basic_tokenize unconditionally
        super().__init__(*args, do_lower_case=False, do_basic_tokenize=False, **kwargs)

        # replace the word piece tokenizer with ours
        self.wordpiece_tokenizer = MaskedWordPieceTokenizer(vocab=self.vocab,
                                                            added_tokens_encoder=self.added_tokens_encoder,
                                                            added_tokens_decoder=self.added_tokens_decoder,
                                                            unk_token=self.unk_token)

    def tokenize(self, tokens, mask=None):
        return self.wordpiece_tokenizer.tokenize(tokens, mask)

    # provide an interface that DecoderVocabulary can like
    @property
    def stoi(self):
        return self.vocab

    @property
    def itos(self):
        return self.ids_to_tokens