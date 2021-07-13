#!python3


import os, sys
import argparse
import time

import torch
import torch.nn as nn
import nonechucks as nc
from torch.utils.tensorboard import SummaryWriter
import pathlib
import numpy as np

import config
from data import DvhShapeNetDataset
import util

os.environ['KMP_DUPLICATE_LIB_OK'] = 'True'

FileDirPath = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(FileDirPath, 'models'))
from dvhNet import dvhNet

writer = SummaryWriter()
flags = None

def print_progress_bar(iteration, total, epoch, total_epochs, loss, decimals=1, length=50, fill='=', printEnd="\r"):
    """
    Call in a loop to create terminal progress bar
    @params:
        iteration   - Required  : current iteration (Int)
        total       - Required  : total iterations (Int)
        epoch       - Required  : current epoch string (Int)
        total_epochs- Required  : total number of epochs (Int)
        loss        - Required  : loss (Tensor)
        decimals    - Optional  : positive number of decimals in percent complete (Int)
        length      - Optional  : character length of bar (Int)
        fill        - Optional  : bar fill character (Str)
        printEnd    - Optional  : end character (e.g. "\r", "\r\n") (Str)
    """
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filledLength = int(length * iteration // total)
    bar = fill * filledLength + '>' + '-' * (length - filledLength)
    print(f'\rEpoch {str(epoch + 1)}/{str(total_epochs)}: |{bar}| {percent}% Complete, Loss: {str(loss)}',
          end=printEnd)
    # Print New Line on Complete
    if iteration == total:
        print()

def train_step(dataloader, model, loss_fn, optimizer, epoch, total_epochs):
    '''train operations for one epoch'''
    # size = len(dataloader.dataset) # number of samples
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    model.to(device)

    for batch_idx, (images, points, y) in enumerate(dataloader):
        images, points, y = images.to(device), points.to(device), y.to(device)
        pred = model(images.float(), points.float())  # predicts on the batch of training data
        reshaped_pred = pred.transpose(1, 2)  # (batch_size, T=8, 1)
        reshaped_pred = reshaped_pred.reshape(
            (config.batch_size, config.resolution, config.resolution, config.resolution))
        loss = loss_fn(reshaped_pred.float(), y.float())  # compute prediction error
        # Backpropagation of predication error
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        loss, current = loss.item(), batch_idx * len(images)  # (batch size)
        print_progress_bar(batch_idx, len(dataloader), epoch, total_epochs, loss)

    return loss


def test(dataloader, model, loss_fn, threshold=0.5, after_epoch=None):
    '''model: with loaded checkpoint or trained parameters'''
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    model.to(device)

    testLosses = []
    objpointcloud = []  # append points from each image-occupancy pair together for one visualization per object
    for batch_idx, (images, points, y) in enumerate(dataloader):
        print("\t----", batch_idx)
        images, points, y = images.to(device), points.to(device), y.to(device)  # points: (batch_size, 3, T)
        pred = model(images.float(), points.float())  # (batch_size, 1, T=16**3=4096)
        # print("\tpred=", pred)
        reshaped_pred = pred.transpose(1, 2).reshape(
            (config.batch_size, config.resolution, config.resolution, config.resolution))
        testLosses.append(loss_fn(reshaped_pred.float(), y.float()).item())
        print(f"\tTest loss: {np.mean(np.asarray(testLosses)):>7f}")

        ## convert prediction to point cloud, then to voxel grid
        indices = torch.nonzero(pred > threshold, as_tuple=True)  # tuple of 3 tensors, each the indices of 1 dimensino
        pointcloud = points[indices[0], :, indices[2]].tolist()  # QUESTION: output same order as input points?
        print("\tpointcloud.shape=", np.array(pointcloud).shape)
        objpointcloud += pointcloud  # array of [x,y,z] where pred > threshold

    objpointcloud = np.array(objpointcloud)
    print("objpointcloud.shape=", objpointcloud.shape)
    if len(objpointcloud) != 0:
        voxel = util.pointcloud2voxel(objpointcloud, config.resolution)
        voxel_fp = f"{flags.save_dir}voxel_grid_e{after_epoch}.jpg" if after_epoch else f"{flags.load_ckpt_dir}voxel_grid.jpg"
        util.draw_voxel_grid(voxel, to_show=False, to_disk=True, fp=voxel_fp)
        binvox_fp = f"{flags.save_dir}voxel_grid_e{after_epoch}.binvox" if after_epoch else f"{flags.load_ckpt_dir}voxel_grid.binvox"
        util.save_to_binvox(voxel, config.resolution, binvox_fp)


if __name__ == "__main__":
    timestamp = str(int(time.time()))
    print(f"################# Timestamp = {timestamp} #################")

    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', type=str, default="train",
                        help="One of 'train' or 'test'")
    parser.add_argument('--save_dir', type=str, default=config.save_dir,
                        help="The directory to store the .pth model files and val/test visualization images.")
    parser.add_argument('--load_ckpt_dir', type=str,
                        help="The directory to load .pth model files from. Required for test mode; used for resuming training for train mode")
    parser.add_argument('--num_epochs', type=int)
    flags, unparsed = parser.parse_known_args()

    if flags.mode == "train":
        print("TRAIN mode")

        training_data = nc.SafeDataset(DvhShapeNetDataset(config.train_dir, config.resolution, single_object=True))
        train_dataloader = torch.utils.data.DataLoader(training_data,
                                                       batch_size=config.batch_size)  # shuffle=True, num_workers=4
        model = dvhNet()
        # summary(model, [(1,3,224,224), (1, 3, 4)])

        flags.save_dir = str(pathlib.Path(flags.save_dir)/f'{timestamp}')
        if not os.path.exists(flags.save_dir):
            os.makedirs(flags.save_dir)
        print("save_dir=", flags.save_dir)
        oldepoch = 0
        if flags.load_ckpt_dir:
            ckpt_fp = util.get_checkpoint_fp(flags.load_ckpt_dir)
            print("load_ckpt_dir's latest checkpoint filepath:", ckpt_fp)
            model.load_state_dict(torch.load(ckpt_fp))
            oldepoch = int(ckpt_fp[ckpt_fp.rindex("_") + 1:-4])

        loss_fn = nn.BCELoss()
        optimizer = torch.optim.SGD(model.parameters(), lr=config.learning_rate)  # weight_decay=1e-5

        epochs = flags.num_epochs
        for epoch_idx in range(oldepoch, oldepoch + epochs):
            print(f"-------------------------------\nEpoch {epoch_idx + 1}")
            loss = train_step(train_dataloader, model, loss_fn, optimizer, epoch_idx,  oldepoch + epochs)
            writer.add_scalar("Loss/train", loss, global_step=epoch_idx)
            if epoch_idx % 100 == 0:
                torch.save(model.state_dict(), str(pathlib.Path(flags.save_dir)/f'dvhNet_weights_{epoch_idx + 1}.pth'))
                # test(train_dataloader, model, loss_fn, after_epoch=epoch_idx + 1)
        torch.save(model.state_dict(), str(pathlib.Path(flags.save_dir) / f'dvhNet_weights_{epoch_idx + 1}.pth'))
        # test(train_dataloader, model, loss_fn, after_epoch=oldepoch + epochs)

        writer.flush()
        writer.close()

        print("################# Done #################")


    elif flags.mode == "test":
        print("TEST mode")
        if not flags.load_ckpt_dir:
            sys.exit("ERROR: Checkpoint directory needed for test mode. Use '--load_ckpt_dir' flag")
        else:
            flags.load_ckpt_dir += '' if flags.load_ckpt_dir.endswith("/") else '/'

        ckpt_fp = util.get_checkpoint_fp(flags.load_ckpt_dir)
        print("load_ckpt_dir's latest checkpoint filepath:", ckpt_fp)
        model = dvhNet()
        model.load_state_dict(torch.load(ckpt_fp))
        test_data = nc.SafeDataset(DvhShapeNetDataset(config.test_dir, config.resolution, single_object=True))
        test_dataloader = torch.utils.data.DataLoader(test_data,
                                                      batch_size=config.batch_size)  # shuffle=True, num_workers=4
        loss_fn = nn.BCELoss()
        test(test_dataloader, model, loss_fn)

        print("################# Done #################")
