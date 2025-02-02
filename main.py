import sys, time, os, argparse, warnings, glob, torch
import tqdm
import utils
from dataset import *
from dino_loss import DINOLoss
from torchvision import models as torchvision_models
from torchvision import datasets, transforms
from ssrst_models import DINOHead
import ssrst_models as models
from encoder import ECAPA_TDNN

import datetime
import time
import math
import json
from pathlib import Path

import numpy as np
from PIL import Image
from regex import D
import torch
import torch.nn as nn
import torch.distributed as dist
import torch.backends.cudnn as cudnn
import torch.nn.functional as F

import warnings
warnings.filterwarnings("ignore")


torchvision_archs = sorted(name for name in torchvision_models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(torchvision_models.__dict__[name]))

def get_args_parser():
    # Training settings
     
    parser = argparse.ArgumentParser(description = "Stage I, self-supervsied speaker recognition with contrastive learning.")
    parser.add_argument('--max_frames',        type=int,   default=300,          help='Max input length to the network, 3.0s')
    parser.add_argument('--train_list',        type=str,   default="/data/voxceleb2/train_mini.txt",           help='Path for Vox2 list, https://www.robots.ox.ac.uk/~vgg/data/voxceleb/meta/train_list.txt')
    parser.add_argument('--val_list',          type=str,   default="/data/voxceleb1/test_mini.txt",           help='Path for Vox_O list, https://www.robots.ox.ac.uk/~vgg/data/voxceleb/meta/veri_test2.txt')
    parser.add_argument('--train_path',        type=str,   default="/data/voxceleb2",           help='Path to the Vox2 set')
    parser.add_argument('--vox_lmdb_path',          type=str,   default="/home/yoos/Downloads/vox1_train_lmdb/data.lmdb",     help='Path to the Vox set')
    parser.add_argument('--musan_lmdb_path',          type=str,   default="/home/yoos/Downloads/musan_lmdb/data.lmdb",     help='Path to the musan set')
    
    parser.add_argument('--val_path',          type=str,   default="/data/voxceleb1/VoxCeleb1/voxceleb1_wav",     help='Path to the Vox_O set')
    parser.add_argument('--musan_path',        type=str,   default="/data/musan",           help='Path to the musan set')
    parser.add_argument('--eval',              dest='eval', action='store_true', help='Do evaluation only')
    parser.add_argument('--input_fdim',            type=int, default=80, help='mel bin')
    parser.add_argument('--n_last_blocks',        type=int,   default=1,          help='use last blocks as output to eval')
    

    #extral data pretrain
    parser.add_argument('--imagenet_pretrain', default=False, type=utils.bool_flag, help="""imagenet_pretrain or not.""")
    parser.add_argument('--audioset_pretrain', default=False, type=utils.bool_flag, help="""audioset_pretrain or not.""")

    # Misc
    parser.add_argument('--output_dir', default="./output", type=str, help='Path to save logs and checkpoints.')
    parser.add_argument('--saveckp_freq', default=5, type=int, help='Save checkpoint every x epochs.')
    parser.add_argument('--seed', default=3407, type=int, help='Random seed.')
    parser.add_argument('--num_workers', default=10, type=int, help='Number of data loading workers per GPU.')
    parser.add_argument("--dist_url", default="env://", type=str, help="""url used to set up
        distributed training; see https://pytorch.org/docs/stable/distributed.html""")
    parser.add_argument("--local_rank", default=0, type=int, help="Please ignore and do not set this argument.")
    parser.add_argument('--model_size', default='tiny224', type=str,
        choices=['tiny224', 'small224', 'base224', 'base384'], help="""Type of optimizer. We recommend using adamw with ViTs.""")

    parser.add_argument('--model_type', default='tiny224', type=str,
        choices=['ASTModel', 'vit_tiny', 'vit_small', 'vit_base'], help="""Type of optimizer. We recommend using adamw with ViTs.""")
    parser.add_argument('--patch_size', default=16, type=int, help="""Size in pixels
        of input square patches - default 16 (for 16x16 patches). Using smaller
        values leads to better performance but requires more memory. Applies only
        for ViTs (vit_tiny, vit_small and vit_base). If <16, we recommend disabling
        mixed precision training (--use_fp16 false) to avoid unstabilities.""")
    parser.add_argument('--out_dim', default=65536, type=int, help="""Dimensionality of
        the DINO head output. For complex and large datasets large values (like 65k) work well.""")
    parser.add_argument('--norm_last_layer', default=True, type=utils.bool_flag,
        help="""Whether or not to weight normalize the last layer of the DINO head.
        Not normalizing leads to better performance but can make the training unstable.
        In our experiments, we typically set this paramater to False with vit_small and True with vit_base.""")
    parser.add_argument('--momentum_teacher', default=0.996, type=float, help="""Base EMA
        parameter for teacher update. The value is increased to 1 during training with cosine schedule.
        We recommend setting a higher value with small batches: for example use 0.9995 with batch size of 256.""")
    parser.add_argument('--use_bn_in_head', default=False, type=utils.bool_flag,
        help="Whether to use batch normalizations in projection head (Default: False)")

    # Temperature teacher parameters
    parser.add_argument('--warmup_teacher_temp', default=0.04, type=float,
        help="""Initial value for the teacher temperature: 0.04 works well in most cases.
        Try decreasing it if the training loss does not decrease.""")
    parser.add_argument('--teacher_temp', default=0.04, type=float, help="""Final value (after linear warmup)
        of the teacher temperature. For most experiments, anything above 0.07 is unstable. We recommend
        starting with the default value of 0.04 and increase this slightly if needed.""")
    parser.add_argument('--warmup_teacher_temp_epochs', default=0, type=int,
        help='Number of warmup epochs for the teacher temperature (Default: 30).')

    # Training/Optimization parameters
    parser.add_argument('--use_fp16', type=utils.bool_flag, default=True, help="""Whether or not
        to use half precision for training. Improves training time and memory requirements,
        but can provoke instability and slight decay of performance. We recommend disabling
        mixed precision if the loss is unstable, if reducing the patch size or if training with bigger ViTs.""")
    parser.add_argument('--weight_decay', type=float, default=0.04, help="""Initial value of the
        weight decay. With ViT, a smaller value at the beginning of training works well.""")
    parser.add_argument('--weight_decay_end', type=float, default=0.4, help="""Final value of the
        weight decay. We use a cosine schedule for WD and using a larger decay by
        the end of training improves performance for ViTs.""")
    parser.add_argument('--clip_grad', type=float, default=3.0, help="""Maximal parameter
        gradient norm if using gradient clipping. Clipping with norm .3 ~ 1.0 can
        help optimization for larger ViT architectures. 0 for disabling.""")
    parser.add_argument('--batch_size_per_gpu', default=2, type=int,
        help='Per-GPU batch-size : number of distinct audios loaded on one GPU.')
    parser.add_argument('--epochs', default=100, type=int, help='Number of epochs of training.')
    parser.add_argument('--freeze_last_layer', default=1, type=int, help="""Number of epochs
        during which we keep the output layer fixed. Typically doing so during
        the first epoch helps training. Try increasing this value if the loss does not decrease.""")
    parser.add_argument("--lr", default=0.0005, type=float, help="""Learning rate at the end of
        linear warmup (highest LR used during training). The learning rate is linearly scaled
        with the batch size, and specified here for a reference batch size of 256.""")
    parser.add_argument("--warmup_epochs", default=10, type=int,
        help="Number of epochs for the linear learning-rate warm up.")
    parser.add_argument('--min_lr', type=float, default=1e-6, help="""Target LR at the
        end of optimization. We use a cosine LR schedule with linear warmup.""")
    parser.add_argument('--optimizer', default='adamw', type=str,
        choices=['adamw', 'sgd', 'lars'], help="""Type of optimizer. We recommend using adamw with ViTs.""")
    parser.add_argument('--drop_path_rate', type=float, default=0.1, help="stochastic depth rate")

    # Multi-crop parameters
    parser.add_argument('--global_crops_scale', type=float, nargs='+', default=3,
        help="second of the cropped audio.Used for large global view cropping. When disabling multi-crop (--local_crops_number 0)")
    parser.add_argument('--local_crops_number', type=int, default=4, help="""Number of small
        local views to generate. Set this parameter to 0 to disable multi-crop training." """)
    parser.add_argument('--local_crops_scale', type=float, nargs='+', default=2,
        help="""second of the cropped audio. Used for small local view cropping of multi-crop.""")


    return parser

def train_one_epoch(student, teacher, teacher_without_ddp, dino_loss, data_loader,
                    optimizer, lr_schedule, wd_schedule, momentum_schedule, epoch,
                    fp16_scaler, args):

    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Epoch: [{}/{}]'.format(epoch, args.epochs)
    load_batch_time, infer_gpu_time, ti, tu, tl = 0, 0, 0, 0, 0
    lt = time.time()
    st = lt
    for it, audios in enumerate(metric_logger.log_every(data_loader, 100, header)):
        #audios 是一个长度为2 + 4（crop_global + crop_local num）的列表。每个元素是平常的bath, (b, 3, len, len)
        # update weight decay and learning rate according to their schedule
        it = len(data_loader) * epoch + it  # global training iteration
        for i, param_group in enumerate(optimizer.param_groups):
            param_group["lr"] = lr_schedule[it]
            if i == 0:  # only the first group is regularized
                param_group["weight_decay"] = wd_schedule[it]

        # move audios to gpu
        audios = [audio.cuda(non_blocking=True) for audio in audios]

        load_batch_time = time.time() - lt
        tl += load_batch_time
        gt = time.time()
        # teacher and student forward passes + compute dino loss
        with torch.cuda.amp.autocast(fp16_scaler is not None):
            teacher_output = teacher(audios[:2])  # only the 2 global views pass through the teacher
            student_output = student(audios)
            loss = dino_loss(student_output, teacher_output, epoch)

        infer_gpu_time = time.time() - gt
        ti += infer_gpu_time
        #print(f"load_batch_time{load_batch_time}s, infer_gpu_time{infer_gpu_time}s")


        if not math.isfinite(loss.item()):
            print("Loss is {}, stopping training".format(loss.item()), force=True)
            sys.exit(1)

        # student update
        optimizer.zero_grad()
        param_norms = None
        if fp16_scaler is None:
            loss.backward()
            if args.clip_grad:
                param_norms = utils.clip_gradients(student, args.clip_grad)
            utils.cancel_gradients_last_layer(epoch, student,
                                              args.freeze_last_layer)
            optimizer.step()
        else:
            fp16_scaler.scale(loss).backward()
            if args.clip_grad:
                fp16_scaler.unscale_(optimizer)  # unscale the gradients of optimizer's assigned params in-place
                param_norms = utils.clip_gradients(student, args.clip_grad)
            utils.cancel_gradients_last_layer(epoch, student,
                                              args.freeze_last_layer)
            fp16_scaler.step(optimizer)
            fp16_scaler.update()

        # EMA update for the teacher
        with torch.no_grad():
            m = momentum_schedule[it]  # momentum parameter
            for param_q, param_k in zip(student.module.parameters(), teacher_without_ddp.parameters()):
                param_k.data.mul_(m).add_((1 - m) * param_q.detach().data)

        # logging
        torch.cuda.synchronize()

        metric_logger.update(loss=loss.item())
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])
        metric_logger.update(wd=optimizer.param_groups[0]["weight_decay"])

        lt = time.time()
        tu += (lt - gt - infer_gpu_time)
    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    tt = time.time() - st
    print(f"total time : {tt} s, load time {tl} s, gpu time {ti} s, update time {tu} s")
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}

def train_dino(args):

    os.makedirs(args.output_dir, exist_ok = True)

    utils.init_distributed_mode(args)
    utils.fix_random_seeds(args.seed)
    print("git:\n  {}\n".format(utils.get_sha()))
    print("\n".join("%s: %s" % (k, str(v)) for k, v in sorted(dict(vars(args)).items())))
    cudnn.benchmark = True

    # ============ preparing data ... ============
    data_loader = get_loader(args) # Define the dataloader
    scorefile = open(args.output_dir + "/scores.txt", "a+")

    #============ building student and teacher networks ... ============
    # if args.model_type in models.__dict__.keys():
    #     if args.model_type == "ASTModel":
    #         student = models.__dict__['ASTModel'](**vars(args))
    #         teacher = models.__dict__['ASTModel'](**vars(args))
    #         embed_dim = student.original_embedding_dim
    #     else:
    #         student = models.__dict__[args.model_type](
    #         patch_size=args.patch_size,
    #         drop_path_rate=args.drop_path_rate,  # stochastic depth
    #     )
    #         teacher = models.__dict__[args.model_type](patch_size=args.patch_size)
    #         embed_dim = student.embed_dim

    student = ECAPA_TDNN()
    teacher = ECAPA_TDNN()
    embed_dim = 192
    # import timm
    # student = timm.create_model('resnet18', pretrained=False)

    # from ThinResNet34 import ThinResNet34
    # student = ThinResNet34()

    #student = ECAPA_TDNN()
            
    # from resnet import ResNet50
    # student = ResNet50()

    # for layers in student.children():
    #     print(layers)


    if args.eval:
        # utils.only_load_model(
        #     os.path.join(args.output_dir, "checkpoint.pth"),  #注意名字
        #     student=student
        # )
        #分布式的时候，是否会有影响？
        EER, minDCF = evaluate_network(student, **vars(args))
        print(time.strftime("%Y-%m-%d %H:%M:%S"), "EER %2.4f, minDCF %.3f"%(EER, minDCF))
        return
    # multi-crop wrapper handles forward with inputs of different resolutions
    student = utils.MultiCropWrapper(student, DINOHead(
        embed_dim,
        args.out_dim,
        use_bn=args.use_bn_in_head,
        norm_last_layer=args.norm_last_layer, #可选，通常true 结果会比较好，但训不稳定
    ))
    teacher = utils.MultiCropWrapper(
        teacher,
        DINOHead(embed_dim, args.out_dim, args.use_bn_in_head),
    )
    student = ECAPA_TDNN()
    teacher = ECAPA_TDNN()
    utils.load_pretrained_weights(student, "output/Best_EER.pth", checkpoint_key="student", model_name=None, patch_size=None)
    EER1, minDCF1 = evaluate_network(student, **vars(args))
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "EER %2.4f, minDCF %.3f"%(EER1, minDCF1))
    exit()

    # move networks to gpu
    student, teacher = student.cuda(), teacher.cuda()
    # synchronize batch norms (if any)
    if utils.has_batchnorms(student):
        student = nn.SyncBatchNorm.convert_sync_batchnorm(student)
        teacher = nn.SyncBatchNorm.convert_sync_batchnorm(teacher)

        # we need DDP wrapper to have synchro batch norms working...
        teacher = nn.parallel.DistributedDataParallel(teacher, device_ids=[args.gpu])  # find_unused_parameters=True才能训，不然untimeError: Expected to have finished reduction in the prior iteration before starting a new one。是否会影响结果？ 如何才能去掉？ 和网络结构有关，deit用，vit不用
        teacher_without_ddp = teacher.module
    else:
        # teacher_without_ddp and teacher are the same thing
        teacher_without_ddp = teacher
    student = nn.parallel.DistributedDataParallel(student, device_ids=[args.gpu])
    # teacher and student start with the same weights
    teacher_without_ddp.load_state_dict(student.module.state_dict())
    # there is no backpropagation through the teacher, so no need for gradients
    for p in teacher.parameters():
        p.requires_grad = False
    print(f"Student and Teacher are built: they are both {args.model_type} network.")


        # ============ preparing loss ... ============
    dino_loss = DINOLoss(
        args.out_dim,
        args.local_crops_number + 2,  # total number of crops = 2 global crops + local_crops_number
        args.warmup_teacher_temp,
        args.teacher_temp,
        args.warmup_teacher_temp_epochs,
        args.epochs,
    ).cuda()

    # ============ preparing optimizer ... ============
    params_groups = utils.get_params_groups(student)
    if args.optimizer == "adamw":
        optimizer = torch.optim.AdamW(params_groups)  # to use with ViTs
    elif args.optimizer == "sgd":
        optimizer = torch.optim.SGD(params_groups, lr=0, momentum=0.9)  # lr is set by scheduler
    elif args.optimizer == "lars":
        optimizer = utils.LARS(params_groups)  # to use with convnet and large batches
    # for mixed precision training
    fp16_scaler = None
    if args.use_fp16:
        fp16_scaler = torch.cuda.amp.GradScaler()

    # ============ optionally resume training ... ============
    to_restore = {"epoch": 0}
    utils.restart_from_checkpoint(
        os.path.join(args.output_dir, "checkpoint.pth"),  #注意名字
        run_variables=to_restore,
        student=student,
        teacher=teacher,
        optimizer=optimizer,
        fp16_scaler=fp16_scaler,
        dino_loss=dino_loss,
    )




    # ============ init schedulers ... ============
    lr_schedule = utils.cosine_scheduler(
        args.lr * (args.batch_size_per_gpu * utils.get_world_size()) / 256.,  # linear scaling rule
        args.min_lr,
        args.epochs, len(data_loader),
        warmup_epochs=args.warmup_epochs,
    )
    wd_schedule = utils.cosine_scheduler(
        args.weight_decay,
        args.weight_decay_end,
        args.epochs, len(data_loader),
    )
    # momentum parameter is increased to 1. during training with a cosine schedule
    momentum_schedule = utils.cosine_scheduler(args.momentum_teacher, 1,
                                               args.epochs, len(data_loader))
    print(f"Loss, optimizer and schedulers ready.")

    
    start_epoch = to_restore["epoch"]

    start_time =  begin_epoch_time = time.time()
    avg_epoch_time, n = 0, 0
    Best_EER, Best_minDCF = 1e9, 1e9
    print("Starting DINO training !")

    for epoch in range(start_epoch, args.epochs):
        data_loader.sampler.set_epoch(epoch)
        # ============ training one epoch of DINO ... ============
        train_stats = train_one_epoch(student, teacher, teacher_without_ddp, dino_loss,
            data_loader, optimizer, lr_schedule, wd_schedule, momentum_schedule,
            epoch, fp16_scaler, args)

        # ============ writing logs ... ============
        save_dict = {
            'student': student.state_dict(),
            'teacher': teacher.state_dict(),
            'optimizer': optimizer.state_dict(),
            'epoch': epoch + 1,
            'args': args,
            'dino_loss': dino_loss.state_dict(),
        }
        if fp16_scaler is not None:
            save_dict['fp16_scaler'] = fp16_scaler.state_dict()
        utils.save_on_master(save_dict, os.path.join(args.output_dir, 'checkpoint.pth'))  #时刻保存最新模型
        if args.saveckp_freq and epoch % args.saveckp_freq == 0:
            utils.save_on_master(save_dict, os.path.join(args.output_dir, f'checkpoint{epoch:04}.pth'))
            
            #分布式的时候，是否会有影响？
            #eval_model = models.__dict__[args.model_type ](**vars(args))   #注意这里模型类型

            eval_model = ECAPA_TDNN()
            utils.load_pretrained_weights(eval_model, os.path.join(args.output_dir, 'checkpoint.pth'), checkpoint_key="student", model_name=None, patch_size=None)
            EER, minDCF = evaluate_network(eval_model, **vars(args))
            print(time.strftime("%Y-%m-%d %H:%M:%S"), "EER %2.4f, minDCF %.3f"%(EER, minDCF))
            del eval_model
            torch.cuda.empty_cache()
            
            print(time.strftime("%Y-%m-%d %H:%M:%S"), "EER %2.4f, minDCF %.3f"%(EER, minDCF))
            scorefile.write("Epoch %d, EER %2.4f, minDCF %.3f\n"%(epoch, EER, minDCF))
            scorefile.flush() #一般的文件流操作都包含缓冲机制，write方法并不直接将数据写入文件，而是先写入内存中特定的缓冲区。flush方法是用来刷新缓冲区的，即将缓冲区中的数据立刻写入文件，同时清空缓冲区。
            if EER < Best_EER:
                Best_EER = EER
                utils.save_on_master(save_dict['student'], os.path.join(args.output_dir, f'Best_EER.pth')) #只保存student
            if minDCF < Best_minDCF:
                Best_minDCF = minDCF
                utils.save_on_master(save_dict['student'], os.path.join(args.output_dir, f'Best_minDCF.pth'))

        # Otherwise, recored the training loss and acc
        log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                     'epoch': epoch}
        if utils.is_main_process():
            with (Path(args.output_dir) / "log.txt").open("a") as f:
                f.write(json.dumps(log_stats) + "\n")

        train_one_epoch_time = (time.time() - begin_epoch_time) / 3600.0
        #avg_epoch_time = epoch / (epoch + 1) * avg_epoch_time + train_one_epoch_time / (epoch + 1)
        avg_epoch_time = avg_epoch_time + 1.0 / (n + 1) * (train_one_epoch_time - avg_epoch_time)
        n += 1
        print(f"Estimate remaining training time: {(args.epochs - epoch - 1) * avg_epoch_time} hours")  
        begin_epoch_time = time.time()
    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))



def evaluate_network(model, val_list, val_path, max_frames, input_fdim, n_last_blocks=1, **kwargs):
        model.eval()
        files, feats = [], {}
        for line in open(val_list).read().splitlines():
            data = line.split()
            files.append(data[1])
            files.append(data[2])
        setfiles = list(set(files))
        setfiles.sort()  # Read the list of wav files
        for idx, file in tqdm.tqdm(enumerate(setfiles), total = len(setfiles)):

            audio = eval_transform(os.path.join(val_path, file))

            with torch.no_grad():
                feat = model(audio)
                #print(feat)
                ref_feat = feat.detach().cpu()
                #ref_feat = torch.cat([x[:, 0] for x in intermediate_output], dim=-1).detach().cpu()
                #ref_feat = model(feat).detach().cpu()
            feats[file]  = ref_feat # Extract features for each data, get the feature dict
        scores, labels  = [], []
        for line in open(val_list).read().splitlines():
            data = line.split()
            ref_feat = F.normalize(feats[data[1]].cuda(), p=2, dim=1) # feature 1
            com_feat = F.normalize(feats[data[2]].cuda(), p=2, dim=1) # feature 2
            score = numpy.mean(torch.matmul(ref_feat, com_feat.T).detach().cpu().numpy()) # Get the score
            scores.append(score)
            labels.append(int(data[0]))
        EER = utils.tuneThresholdfromScore(scores, labels, [1, 0.1])[1]
        fnrs, fprs, thresholds = utils.ComputeErrorRates(scores, labels)
        minDCF, _ = utils.ComputeMinDcf(fnrs, fprs, thresholds, 0.05, 1, 1)
        return EER, minDCF

if __name__ == '__main__':
    parser =  get_args_parser()
    args = parser.parse_args()
    train_dino(args)
    