#!/bin/bash
CUDA_VISIBLE_DEVICES=0 python  main.py \
--model_type vit_tiny \
--use_fp16 False \
--lr 0.2 \
--min_lr 5e-5 \
--batch_size 16 \
--musan_lmdb_path /home/yoos/Downloads/musan_lmdb/data.lmdb \
--train_list list/voxceleb1_train_list \
--val_list list/trials.txt \
--vox_lmdb_path /home/yoos/Downloads/vox1_train_lmdb/data.lmdb \
--train_path /data/voxceleb \
--val_path /data/voxceleb/voxceleb1 \
--musan_path /data/musan_split \
--saveckp_freq 1 \
--imagenet_pretrain False \
--audioset_pretrain False \
--local_crops_number 0 \
--epochs 100 \
--warmup_epochs 10 > log.txt