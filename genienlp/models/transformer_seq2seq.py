#
# Copyright (c) 2020 The Board of Trustees of the Leland Stanford Junior University
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

import logging
import torch
from transformers import AutoModelForSeq2SeqLM, AutoConfig
from loss_dropper import LossDropper

from ..data_utils.numericalizer import TransformerNumericalizer
from ..util import get_mbart_lang
from .base import GenieModel

logger = logging.getLogger(__name__)


class TransformerSeq2Seq(GenieModel):
    def __init__(self, config=None, *inputs, args, tasks, vocab_sets, save_directory=None, **kwargs):
        config = AutoConfig.from_pretrained(args.pretrained_model, cache_dir=args.embeddings)
        super().__init__(config)
        self.args = args
        args.dimension = config.d_model
        self._is_bart_large = self.args.pretrained_model == 'facebook/bart-large'
        self._is_mbart = 'mbart' in self.args.pretrained_model
        
        if save_directory is not None:
            self.model = AutoModelForSeq2SeqLM.from_config(config)
        else:
            self.model = AutoModelForSeq2SeqLM.from_pretrained(self.args.pretrained_model,
                                                               cache_dir=self.args.embeddings)
            
        self.numericalizer = TransformerNumericalizer(self.args.pretrained_model, max_generative_vocab=None,
                                                      preprocess_special_tokens=args.preprocess_special_tokens)

        self.init_vocab_from_data(vocab_sets, tasks, save_directory)
        self.model.resize_token_embeddings(self.numericalizer.num_tokens)

        if args.dropper_ratio > 0:
            self.dropper = LossDropper(dropc=args.dropper_ratio, min_count=args.dropper_min_count)
        else:
            self.dropper = None
            
            
    def add_new_vocab_from_data(self, tasks, resize_decoder=False):
        super().add_new_vocab_from_data(tasks, resize_decoder)
        self.model.resize_token_embeddings(self.numericalizer.num_tokens)
    
    
    def set_decoder_start_token_id(self, lang):
        if self._is_mbart:
            # mBART, in contrast to MT5 or XLM-R, needs language id
            # For now we only support single language training and evaluation with mbart models
            lang_id = get_mbart_lang(lang)
            self.model.config.decoder_start_token_id = self.numericalizer._tokenizer.lang_code_to_id[lang_id]


    def forward(self, *input, **kwargs):
        if self.training:
            batch = input[0]

            answer = batch.answer.value
            answer_length = batch.answer.length
            if self._is_bart_large:
                # remove BOS from the answer to BART-Large because BART-Large was not trained to predict BOS
                # (unlike BART-Base or mBART)
                #
                # NOTE: various people at Huggingface and elsewhere have tried to conclusively ascertain
                # whether BOS should be there or not, and the answer seems to be that BOS should not be there
                # at all, either in input or in the output
                # but empirically, BOS in the input works slightly better, pehraps because our sentences start
                # with a lowercase letter, so we leave it
                answer = answer[:, 1:].contiguous()
                answer_length = answer_length - 1

            # setting pad output tokens to -100 means they will be ignored in calculating loss
            answer[answer==self.numericalizer.pad_id] = -100

            # this is similar to what `transformers` Seq2Seq models do, but with two changes
            # (1) loss is averaged over sequence lengths first, then over the batch size. This way,
            # longer sequences in the batch do not drown shorter sequences.
            # (2) if `args.dropper_ratio > 0.0`, will perform Loss Truncation
            outputs = self.model(batch.context.value, labels=answer, attention_mask=(batch.context.value!=self.numericalizer.pad_id))
            ce_loss_fct = torch.nn.CrossEntropyLoss(reduction='none')
            loss = ce_loss_fct(outputs.logits.transpose(1, 2), answer)
            loss = loss.sum(dim=1) / answer_length # accounts for the case where BOS is removed
            if self.dropper is not None:
                dropper_mask = self.dropper(loss)
                loss = loss * dropper_mask
            loss = loss.mean() # average over the batch size
            outputs.loss = loss # replace the loss calculated by `transformers` with the new loss
            return outputs
        else:
            return self.model(**kwargs)

    def generate(self,
                 batch,
                 max_output_length,
                 num_outputs,
                 temperature,
                 repetition_penalty,
                 top_k,
                 top_p,
                 num_beams,
                 num_beam_groups,
                 diversity_penalty,
                 no_repeat_ngram_size,
                 do_sample
                 ):
        
        decoder_start_token_id = None
        if self._is_mbart:
            decoder_start_token_id = self.model.config.decoder_start_token_id

        input_ids = batch.context.value
        # when attention_mask is not provided to generate(), it will default to masking pad tokens, which is the correct thing
        generated = self.model.generate(input_ids=input_ids,
                                        max_length=max_output_length,
                                        min_length=2, # generate at least one token after BOS
                                        bos_token_id=self.numericalizer._tokenizer.bos_token_id,
                                        pad_token_id=self.numericalizer._tokenizer.pad_token_id,
                                        early_stopping=True,
                                        num_return_sequences=num_outputs,
                                        repetition_penalty=repetition_penalty,
                                        temperature=temperature,
                                        eos_token_id=self.numericalizer._tokenizer.eos_token_id,
                                        top_k=top_k,
                                        top_p=top_p,
                                        num_beams=num_beams,
                                        num_beam_groups=num_beam_groups,
                                        diversity_penalty=diversity_penalty,
                                        no_repeat_ngram_size=no_repeat_ngram_size,
                                        do_sample=do_sample,
                                        decoder_start_token_id=decoder_start_token_id
                                        )

        return generated


    def confidence(self, batch, predictions, prediction_lengths, mc_dropout=False, mc_dropout_num=1) -> List[ConfidenceOutput]:
        """
        predictions: Tensor of shape (batch_size, output_length)
        mc_dropout: if True, will activate dropout layers
        mc_droput_num: number of Monte Carlo samples used for the MC Dropout method
        """
        assert mc_dropout or mc_dropout_num == 1, 'MC Dropout is disabled, but mc_droput_num is not 1'

        batch_size = predictions.shape[0]
        repetition_factor = batch_size//batch.context.value.shape[0]
        input_ids = batch.context.value.repeat_interleave(repetition_factor, dim=0) # repeat to account for multiple predictions per input

        pad_token_id = self.numericalizer._tokenizer.pad_token_id
        attention_mask = self.bart._prepare_attention_mask_for_generation(input_ids=input_ids, pad_token_id=pad_token_id, eos_token_id=self.numericalizer._tokenizer.eos_token_id)
        truncated_predictions = predictions[:, 1:] # remove the BOS token since it is not actually being generated
        # output_mask = truncated_predictions.ne(pad_token_id).long()

        batch_nodrop_logits = []
        outputs = self.bart(input_ids=input_ids, decoder_input_ids=predictions, attention_mask=attention_mask, return_dict=True, use_cache=False)
        logits = outputs.logits[:, :-1, :] # remove the last probability distribution which is for the token after EOS
        for i in range(batch_size):
            batch_nodrop_logits.append(logits[i].gather(dim=1, index=truncated_predictions[i].view(-1, 1)).view(-1))
        
        # activate dropout layers
        should_revert_to_eval = True
        if mc_dropout:
            if self.training:
                # already in training mode, so no need to change it back at the end of the function
                should_revert_to_eval = False
            self.train()

        batch_logits = [[] for _ in range(batch_size)]
        for _ in range(mc_dropout_num):
            outputs = self.bart(input_ids=input_ids, decoder_input_ids=predictions, attention_mask=attention_mask, return_dict=True, use_cache=False)
            logits = outputs.logits[:, :-1, :] # remove the last probability distribution which is for the token after EOS
            for i in range(batch_size):
                batch_logits[i].append((logits[i].gather(dim=1, index=truncated_predictions[i].view(-1, 1)).view(-1))[:prediction_lengths[i]])

        confidences = []
        for i in range(batch_size):
            confidences.append(
                        ConfidenceOutput(logits=batch_logits[i],
                                         gold_answer=batch.answer.value[i//repetition_factor][:batch.answer.length[i//repetition_factor]],
                                         prediction=predictions[i][:prediction_lengths[i]+1],  # +1 to include EOS
                                         nodrop_logits=batch_nodrop_logits[i][:prediction_lengths[i]],
                                         ))

        # return the model back to its previous state
        if mc_dropout and should_revert_to_eval:
            self.eval()
        
        return confidences