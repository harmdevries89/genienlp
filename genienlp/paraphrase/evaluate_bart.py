import argparse
from pathlib import Path

import torch
from tqdm import tqdm

from transformers import BartForConditionalGeneration, BartTokenizer
from genienlp.paraphrase.train_bart import BartSystem


DEFAULT_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


def chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def generate_summaries(
    examples: list, out_file: str, model_name: str, batch_size: int = 8, device: str = DEFAULT_DEVICE
):
    fout = Path(out_file).open("w")
    # b = BartSystem.load_from_checkpoint('./workdir/models/bart-large-mw6/checkpointepoch=1.ckpt')
    # b.model.save_pretrained('./workdir/models/bart-large-mw6/')
    # b.tokenizer.save_pretrained('./workdir/models/bart-large-mw6/')
    model = BartForConditionalGeneration.from_pretrained(model_name).to(device)
    model.eval()
    model = model.to(device)
    tokenizer = BartTokenizer.from_pretrained(model_name)

    max_length = 140
    min_length = 1

    for batch in tqdm(list(chunks(examples, batch_size))):
        dct = tokenizer.batch_encode_plus(batch, max_length=1024, return_tensors="pt", pad_to_max_length=True)
        # bad = ['which', 'Which', 'restaurant', 'restaurants']
        # bad = [tokenizer.encode(b, add_prefix_space=True, add_special_tokens=False) for b in bad]
        summaries = model.generate(
            input_ids=dct["input_ids"].to(device),
            attention_mask=dct["attention_mask"].to(device),
            num_beams=1,
            do_sample=False,
            temperature=1,
            length_penalty=1,
            max_length=max_length + 2,  # +2 from original because we start at step=1 and stop before max_length
            min_length=min_length + 1,  # +1 from original because we start at step=1
            no_repeat_ngram_size=3,
            early_stopping=True,
            decoder_start_token_id=model.config.eos_token_id,
            num_return_sequences=1
            # bad_words_ids=bad
        )
        # print(bad)
        # print(summaries)
        dec = [tokenizer.decode(g, skip_special_tokens=True, clean_up_tokenization_spaces=True) for g in summaries]
        for hypothesis in dec:
            fout.write(hypothesis + "\n")
            fout.flush()


def run_generate():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "source_path", type=str, help="like cnn_dm/test.source",
    )
    parser.add_argument(
        "output_path", type=str, help="where to save summaries",
    )
    parser.add_argument(
        "model_name", type=str, default="bart-large-cnn", help="like bart-large-cnn",
    )
    parser.add_argument(
        "--device", type=str, required=False, default=DEFAULT_DEVICE, help="cuda, cuda:1, cpu etc.",
    )
    parser.add_argument(
        "--bs", type=int, default=8, required=False, help="batch size: how many to summarize at a time",
    )
    args = parser.parse_args()
    examples = [" " + x.rstrip() for x in open(args.source_path).readlines()]
    generate_summaries(examples, args.output_path, args.model_name, batch_size=args.bs, device=args.device)


if __name__ == "__main__":
    run_generate()