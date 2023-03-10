import argparse
import logging
import os
import gc
import random
import sys
import cv2
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as transforms
import torchvision.transforms.functional as TF
from pathlib import Path
from torch import optim
from torch.utils.data import DataLoader, random_split
import torch.cuda.comm
import psutil
from tqdm import tqdm
import numpy as np

import wandb
from evaluate import evaluate
from unet import UNet
from utils.data_loading import BasicDataset, CarvanaDataset
from utils.dice_score import dice_loss

dir_img = Path('./data/imgs/')
dir_mask = Path('./data/masks/')
dir_checkpoint = Path('./checkpoints/')

def train_model(
        model,
        device,
        epochs: int = 5,
        batch_size: int = 1,
        learning_rate: float = 1e-5,
        val_percent: float = 0.1,
        save_checkpoint: bool = True,
        img_scale: float = 0.5,
        amp: bool = False,
        opt: str = "rmsprop",
        weight_decay: float = 1e-8,
        momentum: float = 0.999,
        gradient_clipping: float = 1.0,
        save: str = None,
        export: str = "sigmoid",
        augment: str = None,
        project_name: str = None

):
    pred_count = 0
    
    # 1. Create dataset
    try:
        dataset = CarvanaDataset(dir_img, dir_mask, img_scale, augment=augment)
    except (AssertionError, RuntimeError):
        dataset = BasicDataset(dir_img, dir_mask, img_scale, augment=augment)

    # 2. Split into train / validation partitions
    n_val = int(len(dataset) * val_percent)
    n_train = len(dataset) - n_val
    train_set, val_set = random_split(dataset, [n_train, n_val], generator=torch.Generator().manual_seed(0))

    # 3. Create data loaders
    loader_args = dict(batch_size=batch_size, num_workers=4, pin_memory=True)
    train_loader = DataLoader(train_set, shuffle=True, **loader_args)
    val_loader = DataLoader(val_set, shuffle=False, drop_last=True, **loader_args)

    # (Initialize logging)
    experiment = wandb.init(project='cspine-research-unet', name=project_name, resume='allow')
    experiment.config.update(
        dict(epochs=epochs, batch_size=batch_size, learning_rate=learning_rate,
             val_percent=val_percent, save_checkpoint=save_checkpoint, img_scale=img_scale, amp=amp,
             optimizer=opt, weight_decay=weight_decay)
    )

    logging.info(f'''Starting training:
        Epochs:          {epochs}
        Batch size:      {batch_size}
        Learning rate:   {learning_rate}
        Training size:   {n_train}
        Validation size: {n_val}
        Checkpoints:     {save_checkpoint}
        Device:          {device.type}
        Images scaling:  {img_scale}
        Mixed Precision: {amp}
    ''')
    
    # 4. Set up the optimizer, the loss, the learning rate scheduler and the loss scaling for AMP
    # optimizer ????????????
    if opt == "rmsprop":
        optimizer = optim.RMSprop(model.parameters(),
                              lr=learning_rate, weight_decay=weight_decay, momentum=momentum, foreach=True)
    elif opt == "adam":
        optimizer = optim.Adam(model.parameters(), lr=learning_rate, eps=1e-8, weight_decay=weight_decay, foreach=True)

    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'max', patience=5)  # goal: maximize Dice score
    grad_scaler = torch.cuda.amp.GradScaler(enabled=amp)
    criterion = nn.CrossEntropyLoss() if model.module.n_classes > 1 else nn.BCEWithLogitsLoss()
    global_step = 0

    if not os.path.isdir("outputs"):
        os.mkdir("outputs")
    if not os.path.isdir("checkpoints"):
        os.mkdir("checkpoints")
    if not os.path.isdir(os.path.join("outputs", save)):
        os.mkdir(os.path.join("outputs", save))
        os.mkdir(os.path.join("checkpoints", save))
    
    # 5. Begin training
    for epoch in range(1, epochs + 1):
        model.train()
        epoch_loss = 0
        with tqdm(total=n_train, desc=f'Epoch {epoch}/{epochs}', unit='img') as pbar:
            for batch in train_loader:
                images, true_masks = batch['image'], batch['mask']

                assert images.shape[1] == model.module.n_channels, \
                    f'Network has been defined with {model.module.n_channels} input channels, ' \
                    f'but loaded images have {images.shape[1]} channels. Please check that ' \
                    'the images are loaded correctly.'

                images = images.to(device=device, dtype=torch.float32, memory_format=torch.channels_last)
                true_masks = true_masks.to(device=device, dtype=torch.long)

                with torch.autocast(device.type if device.type != 'mps' else 'cpu', enabled=amp):
                    masks_pred = model(images)
                    
                    # ?????? ?????? labeling??? mask(??????)
                    origin = np.asarray(TF.to_pil_image(true_masks[0].float().cpu())).copy()
                    origin[origin > 0] = 255
                    origin = cv2.cvtColor(origin, cv2.COLOR_GRAY2BGR) # color channel??? 1???????????? BGR??? ??????
                    
                    # visualize??? ?????? ????????????
                    if export == "sigmoid":
                        pred = torch.sigmoid(masks_pred)
                        pred = torch.where(pred > 0.5, 1, 0).squeeze(0)
                    elif export == "mean":
                        m = torch.mean(torch.where(masks_pred > 0, masks_pred, 0))
                        pred = torch.where(masks_pred > m, 1, 0).squeeze(0)

                    # ????????? ????????? mask(??????)
                    pred = np.asarray(TF.to_pil_image(pred.float().cpu())).copy()
                    pred = cv2.cvtColor(pred, cv2.COLOR_GRAY2BGR) # color channel??? 1???????????? BGR??? ??????
                    
                    # concatenate (??? ????????? ????????? ?????????)
                    # res = np.concatenate((origin, pred), axis=1)
                    # cv2.imwrite(os.path.join("outputs", save, f"{pred_count}.jpg"), res)
                    pred_count += 1

                    # loss ???????????? -> ?????? ??????????????? class??? ?????????
                    if model.module.n_classes == 1:
                        loss = criterion(masks_pred.squeeze(1), true_masks.float())
                        loss += dice_loss(F.sigmoid(masks_pred.squeeze(1)), true_masks.float(), multiclass=False)
                    else:
                        loss = criterion(masks_pred, true_masks)
                        loss += dice_loss(
                            F.softmax(masks_pred, dim=1).float(),
                            F.one_hot(true_masks, model.module.n_classes).permute(0, 3, 1, 2).float(),
                            multiclass=True
                        )

                optimizer.zero_grad(set_to_none=True)
                # loss.backward()
                grad_scaler.scale(loss).backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), gradient_clipping)
                # optimizer.step()
                grad_scaler.step(optimizer)
                grad_scaler.update()

                pbar.update(images.shape[0])
                global_step += 1
                epoch_loss += loss.item()
                experiment.log({
                    'train loss': loss.item(),
                    'step': global_step,
                    'epoch': epoch
                })
                pbar.set_postfix(**{'loss (batch)': loss.item()})

                # Evaluation round
                division_step = (n_train // (5 * batch_size))
                if division_step > 0:
                    if global_step % division_step == 0:

                        # histograms = {}
                        # for tag, value in model.named_parameters():
                        #     tag = tag.replace('/', '.')
                        #     if not torch.isinf(value).any():
                        #         histograms['Weights/' + tag] = wandb.Histogram(value.data.cpu())
                        #     if not torch.isinf(value.grad).any():
                        #         histograms['Gradients/' + tag] = wandb.Histogram(value.grad.data.cpu())

                        val_score = evaluate(model, val_loader, device, amp)
                        scheduler.step(val_score)

                        logging.info('Validation Dice score: {}'.format(val_score))
                        try:
                            if global_step % 5 == 0:
                                experiment.log({
                                    'learning rate': optimizer.param_groups[0]['lr'],
                                    'validation Dice': val_score,
                                    'images': wandb.Image(images[0].cpu()),
                                    'masks': {
                                        'true': wandb.Image(true_masks[0].float().cpu()),
                                        'pred': wandb.Image(pred),
                                        # 'pred': wandb.Image(masks_pred.argmax(dim=1)[0].float().cpu()),
                                    },
                                    'step': global_step,
                                    'epoch': epoch,
                                    # **histograms
                                })
                            else:
                                experiment.log({
                                    'learning rate': optimizer.param_groups[0]['lr'],
                                    'validation Dice': val_score,
                                    'step': global_step,
                                    'epoch': epoch,
                                    # **histograms
                                })
                        except:
                            pass

        if save_checkpoint and epoch % 5 == 4:
            Path(dir_checkpoint).mkdir(parents=True, exist_ok=True)
            state_dict = model.module.state_dict()
            state_dict['mask_values'] = dataset.mask_values
            torch.save(state_dict, os.path.join("checkpoints", save, 'checkpoint_epoch{}.pth'.format(epoch)))
            logging.info(f'Checkpoint {epoch} saved!')

# python main.py train --epochs 10

def get_args():
    parser = argparse.ArgumentParser(description='Train the UNet on images and target masks')
    parser.add_argument('--epochs', '-e', metavar='E', type=int, default=25, help='Number of epochs')
    parser.add_argument('--batch-size', '-b', dest='batch_size', metavar='B', type=int, default=1, help='Batch size')
    parser.add_argument('--learning-rate', '-l', metavar='LR', type=float, default=1e-5,
                        help='Learning rate', dest='lr')
    parser.add_argument('--load', '-f', type=str, default=False, help='Load model from a .pth file')
    parser.add_argument('--scale', '-s', type=float, default=1, help='Downscaling factor of the images')
    parser.add_argument('--validation', '-v', dest='val', type=float, default=10.0,
                        help='Percent of the data that is used as validation (0-100)')
    parser.add_argument('--amp', action='store_true', default=True, help='Use mixed precision')
    parser.add_argument('--bilinear', action='store_true', default=False, help='Use bilinear upsampling')
    parser.add_argument('--classes', '-c', type=int, default=1, help='Number of classes')

    return parser.parse_args()

def train(save: str = None,
          opt: str = "rmsprop",
          weight_decay: float = 1e-8,
          export="sigmoid",
          augment=None):

    save = "-".join([export, opt, str(weight_decay)])
    args = get_args()

    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    # device = torch.device("cpu")
    logging.info(f'Using device {device}')

    # Change here to adapt to your data
    # n_channels=3 for RGB images
    # n_classes is the number of probabilities you want to get per pixel
    model = UNet(n_channels=3, n_classes=args.classes, bilinear=args.bilinear)
    model = model.to(memory_format=torch.channels_last)

    logging.info(f'Network:\n'
                 f'\t{model.n_channels} input channels\n'
                 f'\t{model.n_classes} output channels (classes)\n'
                 f'\t{"Bilinear" if model.bilinear else "Transposed conv"} upscaling')

    if args.load:
        state_dict = torch.load(args.load, map_location=device)
        del state_dict['mask_values']
        model.load_state_dict(state_dict)
        logging.info(f'Model loaded from {args.load}')

    if torch.cuda.device_count() > 1:
        print("Training with multiple devices")
        model = torch.nn.DataParallel(model, device_ids=range(torch.cuda.device_count()))

    # model??? device??? ?????????
    model = model.to(device)

    gc.collect()
    torch.cuda.empty_cache()

    train_model(
        model=model,
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.lr,
        device=device,
        img_scale=args.scale,
        val_percent=args.val / 100,
        amp=args.amp,
        weight_decay=weight_decay,
        save=save,
        project_name=save,
        augment=augment
    )

    wandb.finish(quiet=True)

if __name__ == '__main__':
    train(opt="rmsprop", weight_decay=1e-8, export="sigmoid")
    # train(opt="rmsprop", weight_decay=1e-7, export="sigmoid")
    # train(opt="rmsprop", weight_decay=1e-6, export="sigmoid")
    # train(opt="rmsprop", weight_decay=1e-8, export="mean")
    # train(opt="rmsprop", weight_decay=1e-7, export="mean")
    # train(opt="rmsprop", weight_decay=1e-6, export="mean")

    # train(opt="adam", weight_decay=1e-8, export="sigmoid")
    # train(opt="adam", weight_decay=1e-7, export="sigmoid")
    # train(opt="adam", weight_decay=1e-6, export="sigmoid")
    # train(opt="adam", weight_decay=1e-8, export="mean")
    # train(opt="adam", weight_decay=1e-7, export="mean")
    # train(opt="adam", weight_decay=1e-6, export="mean")
