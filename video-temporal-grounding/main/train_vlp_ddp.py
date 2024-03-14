import os
import pdb
import sys
import time
import json
import pprint
import random
import numpy as np
from tqdm import tqdm, trange
from collections import defaultdict

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from main.config import BaseOptions, setup_model
from main.dataset import \
    DatasetVLP, start_end_collate_mr, prepare_batch_inputs_mr
from main.inference_mr import eval_epoch, start_inference
from utils.basic_utils import set_seed, AverageMeter, dict_to_markdown
from utils.model_utils import count_parameters

import logging
logger = logging.getLogger(__name__)
logging.basicConfig(format="%(asctime)s.%(msecs)03d:%(levelname)s:%(name)s - %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                    level=logging.INFO)

from distribute_utils import *

def train_epoch(model, criterion, train_loader, optimizer, opt, epoch_i, tb_writer, cls=None):
    logger.info(f"[Epoch {epoch_i+1}]")
    model.train()
    criterion.train()

    # init meters
    time_meters = defaultdict(AverageMeter)
    loss_meters = defaultdict(AverageMeter)

    num_training_examples = len(train_loader)
    timer_dataloading = time.time()
    for batch_idx, batch in tqdm(enumerate(train_loader),
                                 desc="Training Iteration",
                                 total=num_training_examples):
        time_meters["dataloading_time"].update(time.time() - timer_dataloading)

        timer_start = time.time()
        model_inputs, targets = prepare_batch_inputs_mr(batch[1], torch.device('cuda'))
        time_meters["prepare_inputs_time"].update(time.time() - timer_start)

        timer_start = time.time()

        if cls is not None:
            model_inputs.update(cls)
        #model_inputs = model_inputs.to('cuda')
        outputs = model(**model_inputs)
        loss_dict = criterion(outputs, targets)
        weight_dict = criterion.weight_dict
        losses = sum(loss_dict[k] * weight_dict[k] for k in loss_dict.keys() if k in weight_dict)
        time_meters["model_forward_time"].update(time.time() - timer_start)

        timer_start = time.time()
        optimizer.zero_grad()
        losses.backward()
        # except:
            # pdb.set_trace()
        if opt.grad_clip > 0:
            nn.utils.clip_grad_norm_(model.parameters(), opt.grad_clip)
        optimizer.step()
        time_meters["model_backward_time"].update(time.time() - timer_start)

        loss_dict["loss_overall"] = float(losses)  # for logging only
        for k, v in loss_dict.items():
            loss_meters[k].update(float(v) * weight_dict[k] if k in weight_dict else float(v))

        timer_dataloading = time.time()

    # print/add logs
    tb_writer.add_scalar("Train/lr", float(optimizer.param_groups[0]["lr"]), epoch_i+1)
    for k, v in loss_meters.items():
        tb_writer.add_scalar("Train/{}".format(k), v.avg, epoch_i+1)

    to_write = opt.train_log_txt_formatter.format(
        time_str=time.strftime("%Y_%m_%d_%H_%M_%S"),
        epoch=epoch_i+1,
        loss_str=" ".join(["{} {:.4f}".format(k, v.avg) for k, v in loss_meters.items()]))
    with open(opt.train_log_filepath, "a") as f:
        f.write(to_write)

    logger.info("Epoch time stats:")
    for name, meter in time_meters.items():
        d = {k: f"{getattr(meter, k):.4f}" for k in ["max", "min", "avg"]}
        logger.info(f"{name} ==> {d}")


def train(model, criterion, optimizer, lr_scheduler, train_dataset, val_dataset, opt):
    if opt.device == "cuda":
        logger.info("CUDA enabled.")
    #model = nn.DataParallel(model).cuda()
    tb_writer = SummaryWriter(opt.tensorboard_log_dir)
    tb_writer.add_text("hyperparameters", dict_to_markdown(vars(opt), max_str_len=None))
    opt.train_log_txt_formatter = "{time_str} [Epoch] {epoch:03d} [Loss] {loss_str}\n"
    opt.eval_log_txt_formatter = "{time_str} [Epoch] {epoch:03d} [Loss] {loss_str} [Metrics] {eval_metrics_str}\n"
    device = torch.device(opt.device)


    num_tasks = get_world_size()
    global_rank = get_rank()
    sampler_rank = global_rank    
    total_batch_size = opt.bsz * num_tasks
    
    num_training_steps_per_epoch = len(train_dataset) // total_batch_size    

    sampler_train = torch.utils.data.DistributedSampler(
        train_dataset, num_replicas=num_tasks, rank=sampler_rank, shuffle=True
    )
    print(f'num_tasks:{num_tasks}')
    print(f'total_batch_size:{total_batch_size}')
    print(f'batch size:{opt.bsz}')
    print("Sampler_train = %s" % str(sampler_train))

    train_loader = DataLoader(
        train_dataset,
        collate_fn=start_end_collate_mr,
        batch_size=opt.bsz,
        num_workers=opt.num_workers,
        sampler=sampler_train,
        pin_memory=False
    )

    model.to(device)
    model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[opt.gpu], find_unused_parameters=True)
    model_without_ddp = model.module
    if ('tal' in opt.train_path) or ('mq' in opt.train_path):
        cls = {
            'src_cls': train_dataset.src_cls.cuda(),
            'src_cls_mask': train_dataset.src_cls_mask.cuda(),}
    else:
        cls = None

    prev_best_score = 0.
    es_cnt = 0
    if opt.start_epoch is None:
        start_epoch = -1 if opt.eval_init else 0
    else:
        start_epoch = opt.start_epoch
    save_submission_filename = "latest_{}_{}_preds.jsonl".format(opt.dset_name, opt.eval_split_name)
    for epoch_i in trange(start_epoch, opt.n_epoch, desc="Epoch"):
        if epoch_i > -1:
            train_epoch(model, criterion, train_loader, optimizer, opt, epoch_i, tb_writer, cls)
            lr_scheduler.step()
        eval_epoch_interval = opt.eval_epoch
        if opt.eval_path is not None and (epoch_i + 1) % eval_epoch_interval == 0:
            with torch.no_grad():
                metrics_no_nms, metrics_nms, eval_loss_meters, latest_file_paths = \
                    eval_epoch(model, val_dataset, opt, save_submission_filename, epoch_i, criterion, tb_writer)

            # log
            to_write = opt.eval_log_txt_formatter.format(
                time_str=time.strftime("%Y_%m_%d_%H_%M_%S"),
                epoch=epoch_i,
                loss_str=" ".join(["{} {:.4f}".format(k, v.avg) for k, v in eval_loss_meters.items()]),
                eval_metrics_str=json.dumps(metrics_no_nms))

            with open(opt.eval_log_filepath, "a") as f:
                f.write(to_write)
            logger.info("metrics_no_nms {}".format(pprint.pformat(metrics_no_nms["brief"], indent=4)))
            if metrics_nms is not None:
                logger.info("metrics_nms {}".format(pprint.pformat(metrics_nms["brief"], indent=4)))

            metrics = metrics_nms if metrics_nms is not None else metrics_no_nms
            for k, v in metrics["brief"].items():
                tb_writer.add_scalar(f"Eval/{k}", float(v), epoch_i+1)

            # stop_score = metrics["brief"]["MR-full-mAP"]
            # pdb.set_trace()
            stop_score = metrics["brief"][opt.main_metric]
            if stop_score > prev_best_score:
                es_cnt = 0
                prev_best_score = stop_score

                checkpoint = {
                    "model": model.state_dict(),
                    "optimizer": optimizer.state_dict(),
                    "lr_scheduler": lr_scheduler.state_dict(),
                    "epoch": epoch_i,
                    "opt": opt
                }
                torch.save(checkpoint, opt.ckpt_filepath.replace(".ckpt", "_best.ckpt"))

                best_file_paths = [e.replace("latest", "best") for e in latest_file_paths]
                logger.info("The checkpoint file has been updated.")
            else:
                es_cnt += 1
                if opt.max_es_cnt != -1 and es_cnt > opt.max_es_cnt:  # early stop
                    with open(opt.train_log_filepath, "a") as f:
                        f.write(f"Early Stop at epoch {epoch_i}")
                    logger.info(f"\n>>>>> Early stop at epoch {epoch_i}  {prev_best_score}\n")
                    break

            # save ckpt
            checkpoint = {
                "model": model.state_dict(),
                "optimizer": optimizer.state_dict(),
                "lr_scheduler": lr_scheduler.state_dict(),
                "epoch": epoch_i,
                "opt": opt
            }
            torch.save(checkpoint, opt.ckpt_filepath.replace(".ckpt", "_latest.ckpt"))

        if (epoch_i + 1) % opt.save_interval == 0 or (epoch_i + 1) % opt.lr_drop == 0:  # additional copies
            checkpoint = {
                "model": model.state_dict(),
                "optimizer": optimizer.state_dict(),
                "epoch": epoch_i,
                "opt": opt
            }
            torch.save(checkpoint, opt.ckpt_filepath.replace(".ckpt", f"_e{epoch_i:04d}.ckpt"))

        if opt.debug:
            break

    tb_writer.close()


def start_training():
    logger.info("Setup config, data and model...")
    opt = BaseOptions().parse()

    init_distributed_mode(opt)

    set_seed(opt.seed)
    dataset_config = dict(
        dset_name=opt.dset_name,
        data_path=opt.train_path,
        v_feat_dirs=opt.v_feat_dirs,
        q_feat_dir=opt.t_feat_dir,
        v_feat_dim=opt.v_feat_dim,
        q_feat_dim=opt.t_feat_dim,
        q_feat_type="last_hidden_state",
        max_q_l=opt.max_q_l,
        max_v_l=opt.max_v_l,
        ctx_mode=opt.ctx_mode,
        data_ratio=opt.data_ratio,
        normalize_v=not opt.no_norm_vfeat,
        normalize_t=not opt.no_norm_tfeat,
        clip_len=opt.clip_length,
        max_windows=opt.max_windows,
        span_loss_type=opt.span_loss_type,
        txt_drop_ratio=opt.txt_drop_ratio,
        use_cache=opt.use_cache,
        add_easy_negative=opt.add_easy_negative,
        easy_negative_only=opt.easy_negative_only,
        feat_root = opt.feat_root
    )

    dataset_config["data_path"] = opt.train_path
    train_dataset = DatasetVLP(**dataset_config)

    if opt.eval_path is not None:

        dataset_config["data_path"] = [(opt.eval_path)]
        dataset_config["txt_drop_ratio"] = 0
        dataset_config["q_feat_dir"] = opt.t_feat_dir.replace("txt_clip_asr", "txt_clip").replace("txt_clip_cap", "txt_clip")  # for pretraining
        # dataset_config["load_labels"] = False  # uncomment to calculate eval loss
        eval_dataset = DatasetVLP(**dataset_config)
    else:
        eval_dataset = None

    if opt.lr_warmup > 0:
        opt.lr_warmup = opt.n_epoch

    model, criterion, optimizer, lr_scheduler = setup_model(opt)


    logger.info(f"Model {model}")
    count_parameters(model)
    logger.info("Start Training...")
    train(model, criterion, optimizer, lr_scheduler, train_dataset, eval_dataset, opt)
    return opt.ckpt_filepath.replace(".ckpt", "_best.ckpt"), opt.eval_split_name, opt.eval_path, opt.debug


if __name__ == '__main__':
    best_ckpt_path, eval_split_name, eval_path, debug = start_training()
    if not debug:
        input_args = ["--resume", best_ckpt_path,
                      "--eval_split_name", eval_split_name,
                      "--eval_path", eval_path]

        import sys
        sys.argv[1:] = input_args
        logger.info("\n\n\nFINISHED TRAINING!!!")
        logger.info("Evaluating model at {}".format(best_ckpt_path))
        logger.info("Input args {}".format(sys.argv[1:]))
        start_inference()
