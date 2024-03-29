'''
PyTorch implementation of Capsule Networks
'''

import argparse
import os
import shutil
import time

import random
import numpy as np
import cv2
from PIL import Image

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
import torchvision.transforms as transforms
import torchvision.utils as vutils
from torch.autograd import Variable
import torch.nn.functional as F
from torch.optim import lr_scheduler

import models.matrixCapsules as capsNet
from dataset.cityscapesDataLoader import cityscapesDataset
import utils

parser = argparse.ArgumentParser(description='PyTorch CapsNet Training')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
            help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=250, type=int, metavar='N',
            help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
            help='manual epoch number (useful on restarts)')
parser.add_argument('--batchSize', default=64, type=int,
            help='mini-batch size (default: 64)')
parser.add_argument('--imageSize', default=128, type=int,
            help='height/width of the input image to the network')
parser.add_argument('--lr', default=0.001, type=float,
            help='learning rate (default: 0.0005)')
parser.add_argument('--r', type=int, default=3,
            help='Number of Routing Iterations')
parser.add_argument('--clip', default=5, type=int,
            help="Gradient Clipping")
parser.add_argument('--net', default='',
            help="path to net (to continue training)")
parser.add_argument('--print-freq', '-p', default=1, type=int, metavar='N',
            help='print frequency (default:1)')
parser.add_argument('--save-dir', dest='save_dir',
            default='save_temp', type=str,
            help='The directory used to save the trained models')
parser.add_argument('--verbose', default = False, type=bool,
            help='Prints certain messages which user can specify if true')
parser.add_argument('--with_reconstruction', action='store_true', default=True,
            help='Net with reconstruction or not')

use_gpu = torch.cuda.is_available()

def main():
    global args
    args = parser.parse_args()
    print(args)

    # Check if the save directory exists or not
    if not os.path.exists(args.save_dir):
        os.makedirs(args.save_dir)

    cudnn.benchmark = True

    # Initialize the data transforms
    data_transforms = {
        'train': transforms.Compose([
            transforms.Resize((args.imageSize, args.imageSize), interpolation=Image.NEAREST),
            transforms.ToTensor(),
        ]),
        'val': transforms.Compose([
            transforms.Resize((args.imageSize, args.imageSize), interpolation=Image.NEAREST),
            transforms.ToTensor(),
        ]),
        'test': transforms.Compose([
            transforms.Resize((args.imageSize, args.imageSize), interpolation=Image.NEAREST),
            transforms.ToTensor(),
        ]),
    }

    # Data Loading
    data_dir = '/media/salman/DATA/General Datasets/cityscapes'
    # json path for class definitions
    json_path = '/home/salman/pytorch/capsNet/dataset/cityscapesClasses.json'

    image_datasets = {x: cityscapesDataset(data_dir, x, data_transforms[x],
                    json_path) for x in ['train', 'val', 'test']}

    dataloaders = {x: torch.utils.data.DataLoader(image_datasets[x],
                                                  batch_size=args.batchSize,
                                                  shuffle=True,
                                                  num_workers=args.workers)
                  for x in ['train', 'val', 'test']}
    dataset_sizes = {x: len(image_datasets[x]) for x in ['train', 'val', 'test']}

    # Get the dictionary for the id and RGB value pairs for the dataset
    classes = image_datasets['train'].classes
    key = utils.disentangleKey(classes)
    num_classes = len(key) + 1
    # +1 for the background class. The +1 is dataset dependant, since some
    # datasets have an intrinsic background class

    lambda_ = 1e-3
    m = 0.2
    A,B,C,D,E,r = 32,32,32,32,num_classes,args.r

    # Initialize the Network
    model = capsNet.CapsNet(A,B,C,D,E,r,use_gpu)

    if use_gpu:
        model.cuda()

    print('The Matrix Capsules Network')
    print(model)

    if args.net:
        model.load_state_dict(torch.load(args.net))
        m = 0.8
        lambda_ = 0.9

    # Define the optimizer
    optimizer = optim.Adam(model.parameters(), lr=args.lr)
    scheduler = lr_scheduler.ReduceLROnPlateau(optimizer, 'max',patience = 1)

    # Initialize the loss function
    # loss_fn = capsNet.MarginLoss(0.9, 0.1, 0.5)

    for epoch in range(args.start_epoch, args.epochs):

        # Train for one epoch
        train(dataloaders['train'], model, optimizer, epoch, key, lambda_,
                m, num_classes)

        # Save checkpoints
        #torch.save(net.state_dict(), '%s/net_epoch_%d.pth' % (args.save_dir, epoch))

def train(train_loader, model, optimizer, epoch, key, lambda_, m, nc):
    '''
        Run one training epoch
    '''
    model.train()
    b = 0
    steps = len(train_loader)//args.batchSize
    for i, (img, gt) in enumerate(train_loader):

        # Generate the class-wise probability vector
        gt_temp = gt * 255
        labels = utils.generatePresenceVector(gt_temp, key).float()
        oneHotGT = utils.generateOneHot(gt_temp, key).float()

        b += 1
        if lambda_ < 1:
            lambda_ += 2e-1/steps
        if m < 0.9:
            m += 2e-1/steps

        optimizer.zero_grad()
        img, labels= Variable(img, requires_grad=True), Variable(labels),
        gt = Variable(gt, requires_grad=False)
        oneHotGT = Variable(oneHotGT, requires_grad=False)
        if use_gpu:
            img = img.cuda()
            labels = labels.cuda()
            gt = gt.cuda()
            oneHotGT = oneHotGT.cuda()

        out, seg = model(img, lambda_)
        outForLoss = out.view(-1, nc*16 + nc) #b,10*16+10
        out_poses, out_labels = outForLoss[:,:-nc],outForLoss[:,-nc:]

        #loss = model.loss(out_labels, labels, m, nc)
        classLoss = model.classLoss(out_labels, labels)

        torch.nn.utils.clip_grad_norm(model.parameters(), args.clip)

        # Pass the output of Matrix Capsule Network to the Segmentation Network
        #segLoss = model.segLoss(seg, oneHotGT)
        segLoss= F.mse_loss(seg, oneHotGT)

        loss = classLoss + 10 * segLoss

        loss.backward()
        optimizer.step()

        print('[%d/%d][%d/%d] Class Loss: %.4f | Segmentation Loss: %.4f | Total Loss: %.4f'
              % (epoch, args.epochs, i, len(train_loader), classLoss.mean().data[0],
                 segLoss.mean().data[0], loss.mean().data[0]))

        utils.displaySamples(img, seg, gt, use_gpu, key)

        # # Generate the target vector from the groundtruth image
        # # Multiplication by 255 to convert from float to unit8
        # target_temp = target * 255
        # label = utils.generateGTmask(target_temp, key)
        # print(torch.max(label))
        #
        # if use_gpu:
        #     data = data.cuda()
        #     label = label.cuda()
        #
        # #gt.view(-1)
        # #print(target)
        # data, label = Variable(data), Variable(label, requires_grad=False)
        # label = label.float()
        # optimizer.zero_grad()
        # if args.with_reconstruction:
        #     output, probs = model(data, label)
        #     loss = F.mse_loss(output, label)
        #     # margin_loss = loss_fn(probs, target)
        #     # loss = reconstruction_alpha * reconstruction_loss + margin_loss
        #
        # # if args.verbose:
        # print(output[0,3000:3020])
        # print(label[0,3000:3020])
        #
        # loss.backward()
        # optimizer.step()
        #
        # print('[%d/%d][%d/%d] Loss: %.4f'
        #       % (epoch, args.epochs, i, len(train_loader), loss.mean().data[0]))
        # if i % args.print_freq == 0:
        # #    vutils.save_image(real_cpu,
        # #            '%s/real_samples.png' % args.save_dir,
        # #            normalize=True)
        # #    #fake = netG(fixed_noise)
        # #    vutils.save_image(fake.data,
        # #            '%s/fake_samples_epoch_%03d.png' % (args.save_dir, epoch),
        # #            normalize=True)
        #     utils.displaySamples(data, output, target, use_gpu, key)

if __name__ == '__main__':
    main()
