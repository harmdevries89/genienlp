IMAGE_NAME := registry.console.elementai.com/snow.semantic/genienlp:latest

build-image:
	docker build -t $(IMAGE_NAME) .

push-image: build-image
	docker push $(IMAGE_NAME)

launch-interactive:
	eai job new \
		--mem 32 --gpu 1 --cpu 4\
		--data snow.hdevries.genie_logs:/logs \
		--data snow.hdevries.genie:/home/genienlp \
		--image $(IMAGE_NAME)\
		-- bash -c "while true; do sleep 3600; done"

launch-job:
		eai job new \
		--mem 32 --gpu 1 --cpu 4\
		--data snow.hdevries.genie_logs:/logs \
		--data snow.hdevries.genie:/home/genienlp \
		--image $(IMAGE_NAME)\
		-- bash -c "python -m genienlp train --train_tasks almond --data data/nathan --val_every 100 --save /logs/nathan_first --train_iterations 60000 --dimension 768 --transformer_hidden 768 --trainable_decoder_embeddings 50 --encoder_embeddings=bert-base-uncased --decoder_embeddings= --seq2seq_encoder=Identity --rnn_layers 1 --transformer_heads 12 --transformer_layers 0 --rnn_zero_state=average --train_encoder_embeddings --transformer_lr_multiply 0.1 --no_commit --almond_has_multiple_programs --train_batch_tokens 2000"