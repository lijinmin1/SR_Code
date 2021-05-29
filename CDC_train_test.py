import torch
import torch.optim as optim
from torch.optim.lr_scheduler import MultiStepLR
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.autograd import Variable
from torchvision import transforms
import numpy as np
import cv2
import pdb

import argparse
import sys
import os
import importlib
import datetime
import tqdm

sys.path.append('../')
sys.path.append('./modules')
os.environ["CUDA_VISIBLE_DEVICES"] = "4"

from TorchTools.DataTools.DataSets import SRDataList, SRDataListAC, RealPairDatasetAC, MixRealBicDataset, MixRealBicDatasetAC
from TorchTools.Functions.Metrics import psnr
from TorchTools.Functions.functional import tensor_block_cat
from TorchTools.LogTools.logger import Logger
from TorchTools.DataTools.Loaders import to_pil_image, to_tensor
from TorchTools.TorchNet.tools import calculate_parameters, load_weights
import pdb
from TorchTools.TorchNet.Losses import get_content_loss, TVLoss, VGGFeatureExtractor, contextual_Loss, L1_loss, GW_loss
from TorchTools.DataTools.DataSets import RealPairDataset
from TorchTools.TorchNet.GaussianKernels import random_batch_gaussian_noise_param
import block as B
import warnings
warnings.filterwarnings("ignore")

local_test = False

# Load Config File
parser = argparse.ArgumentParser()
parser.add_argument('--config_file', type=str, default='./options/realSR_HGSR_MSHR.py', help='')
parser.add_argument('--train_file', type=str, default='CDC_train_test.py', help='')
parser.add_argument('--gpus', type=int, default=1, help='')
parser.add_argument('--gpu_idx', type=str, default="", help='')
args = parser.parse_args()
module_name = os.path.basename(args.config_file).split('.')[0] # realSR_HGSR_MSHR
config = importlib.import_module('options.' + module_name)     # options.realSR_HGSR_MSHR
print('Load Config From: %s' % module_name)

# Set Params
opt = config.parse_config(local_test)
use_cuda = opt.use_cuda
opt.config_file = args.config_file
opt.train_file = args.train_file
opt.gpus = args.gpus
device = torch.device('cuda') if use_cuda else torch.device('cpu')
zero = torch.mean(torch.zeros(1).to(device))
try:
    opt.need_hr_down
    need_hr_down = opt.need_hr_down
except:
    need_hr_down = False
opt.scale = (opt.scala, opt.scala) #H and W is same
logger = Logger(opt.exp_name, opt.exp_name, opt)
epoch_idx = 0

# Prepare Dataset
transform = transforms.Compose([transforms.RandomCrop(opt.size * opt.scala)])
dataset = SRDataList(opt.dataroot, transform=transform, lr_patch_size=opt.size, scala=opt.scala, mode='Y' if opt.in_ch == 1 else 'RGB', train=True)
test_dataset = SRDataList(opt.test_dataroot, lr_patch_size=opt.size, scala=opt.scala, mode='Y' if opt.in_ch == 1 else 'RGB', train=False)
loader = DataLoader(dataset, batch_size=opt.batch_size, shuffle=True, num_workers=opt.workers, drop_last=True)
test_loader = DataLoader(test_dataset, batch_size=1, shuffle=False, num_workers=opt.workers, drop_last=True)
batches = len(loader)

# SR Model
print('...Build Generator Net...')
if opt.model == 'HGSR':
    sys.path.append('./modules/HourGlassSR')
    import model
    generator = model.HourGlassNet(in_nc=opt.in_ch, out_nc=opt.out_ch, upscale=opt.scala,
                                   nf=64, res_type=opt.res_type, n_mid=2,
                                   n_HG=opt.n_HG, inter_supervis=opt.inter_supervis)
elif opt.model == 'HGSR-MHR':
    sys.path.append('./modules/HourGlassSR')
    import model
    generator = model.HourGlassNetMultiScaleInt(in_nc=opt.in_ch, out_nc=opt.out_ch, upscale=opt.scala,
                                   nf=64, res_type=opt.res_type, n_mid=2, 
                                   n_HG=opt.n_HG, inter_supervis=opt.inter_supervis,
                                   mscale_inter_super=opt.mscale_inter_super)
else:
    generator = SRResNet(scala=opt.scala, inc=opt.in_ch, norm_type=opt.sr_norm_type)
print('%s Created, Parameters: %d' % (generator.__class__.__name__,calculate_parameters(generator)))
print(generator)
generator = load_weights(generator, opt.pretrain, opt.gpus, init_method='kaiming', scale=0.1, just_weight=False, strict=True)
generator = generator.to(device)

print('...Initial Loss Criterion...')
content_criterion = get_content_loss(opt.loss, nn_func=False, use_cuda=use_cuda)

# Init Optim
optim_ = optim.Adam(filter(lambda p: p.requires_grad, generator.parameters()), lr=opt.generatorLR)
scheduler = MultiStepLR(optim_, milestones=opt.decay_step, gamma=0.5)
opt.epochs = int(opt.decay_step[-1] // len(loader)) - epoch_idx
#change learning rate
for _ in range(epoch_idx * len(loader)):
    scheduler.step()

for epoch in range(opt.epochs):
    ''' testing and saving '''
    if epoch % opt.test_interval == 0:
        logger.save('%s_X%d_%d.pth'
                    % (opt.model, opt.scala, epoch + epoch_idx), generator, optim_)
        psnr_sum = 0
        loss_sum = 0
        cnt = 0
        result = []
        flat_mask = []
        edge_mask = []
        corner_mask = []

        generator.eval()
        with torch.no_grad():
            for batch, data in enumerate(test_loader):
                lr = data['LR']
                hr = data['HR']
                lr_var = lr.to(device)
                hr_var = hr.to(device)

                if opt.no_HR:
                    sr_var, SR_map = generator(hr_var)
                else:
                    sr_var, SR_map = generator(lr_var)

                # For HGSR, which returns list
                if isinstance(sr_var, list):
                    sr_var = sr_var[-1]

                if not opt.no_HR:
                    sr_loss = content_criterion(sr_var.detach(), hr_var)
                    psnr_single = psnr(sr_var.detach().cpu().data, hr, peak=opt.rgb_range)
                    psnr_sum += psnr_single

                result.append(sr_var.cpu().data / opt.rgb_range)
                flat_mask.append(SR_map[0].cpu().data)
                edge_mask.append(SR_map[1].cpu().data)
                corner_mask.append(SR_map[2].cpu().data)

        result = torch.cat(result, dim=0)
        result_im = tensor_block_cat(result.unsqueeze(1).clamp_(0, 1))
        result_name = 'epoch_%d_x%d.png' % (epoch + epoch_idx, opt.scala)
        logger.save_training_result(result_name, result_im[0])
        
        flat_mask = torch.cat(flat_mask, dim=0)
        flat_mask_im = tensor_block_cat(flat_mask.unsqueeze(1).clamp_(0, 1))
        flat_mask_name = 'epoch_%d_x%d_flat_mask.png' % (epoch + epoch_idx, opt.scala)
        logger.save_training_result(flat_mask_name, flat_mask_im[0])
        
        edge_mask = torch.cat(edge_mask, dim=0)
        edge_mask_im = tensor_block_cat(edge_mask.unsqueeze(1).clamp_(0, 1))
        edge_mask_name = 'epoch_%d_x%d_edge_mask.png' % (epoch + epoch_idx, opt.scala)
        logger.save_training_result(edge_mask_name, edge_mask_im[0])
        
        corner_mask = torch.cat(corner_mask, dim=0)
        corner_mask_im = tensor_block_cat(corner_mask.unsqueeze(1).clamp_(0, 1))
        corner_mask_name = 'epoch_%d_x%d_corner_mask.png' % (epoch + epoch_idx, opt.scala)
        logger.save_training_result(corner_mask_name, corner_mask_im[0])

        if not opt.no_HR:
            test_info = 'test: psnr: %.4f loss: %.4f' % (psnr_sum / len(test_dataset), sr_loss.item())
            logger.print_log(test_info, with_time=False)
        else:
            logger.print_log('testing...', with_time=False)

    ''' training '''
    start = datetime.datetime.now()
    generator.train()
    error_last = 1e8
    for batch, data in enumerate(loader):

        lr = data['LR']
        hr = data['HR']
        lr_var = lr.to(device)
        hr_var = hr.to(device)
        
        hr_Y = (0.257*hr[:, :1, :, :] + 0.564*hr[:, 1:2, :, :] + 0.098*hr[:, 2:, :, :] + 16/255.0) * 255.0
        map_corner = hr_Y.new(hr_Y.shape).fill_(0)
        map_edge = hr_Y.new(hr_Y.shape).fill_(0)
        map_flat = hr_Y.new(hr_Y.shape).fill_(0)
        hr_Y_numpy = np.transpose(hr_Y.numpy(), (0, 2, 3, 1))
        for i in range(hr_Y_numpy.shape[0]):
            dst = cv2.cornerHarris(hr_Y_numpy[i, :, :, 0], 3, 3, 0.04)
            thres1 = 0.01*dst.max()
            thres2 = -0.001*dst.max()
            map_corner[i, :, :, :] = torch.from_numpy(np.float32(dst > thres1))
            map_edge[i, :, :, :] = torch.from_numpy(np.float32(dst < thres2))
            map_flat[i, :, :, :] = torch.from_numpy(np.float32((dst > thres2) & (dst < thres1)))
        map_corner = map_corner.to(device)
        map_edge = map_edge.to(device)
        map_flat = map_flat.to(device)
        coe_list = []
        coe_list.append(map_flat)
        coe_list.append(map_edge)
        coe_list.append(map_corner)

        # Adding Gaussian Noise To LR
        if opt.add_noise:
            noise, noise_param = random_batch_gaussian_noise_param(lr_var, sig_max=opt.noise_level)
            noise = noise.cuda() if use_cuda else noise
            lr_var = torch.clamp(lr_var + noise, min=0.0, max=1.0)

        train_info = '[%03d/%03d][%04d/%04d] '
        train_info_tuple = [epoch + 1, opt.epochs, batch + 1, batches]
        loss = 0

        sr_var, SR_map = generator(lr_var)

        # Pixel Level Content Loss; For HGSR who has intermediate supervision, need inter loss
        if hasattr(opt, 'inter_supervis') and opt.inter_supervis and isinstance(sr_var, list):
            sr_loss = 0
            for i in range(len(sr_var)):
                if i != len(sr_var) - 1:
                    coe = coe_list[i]
                    single_srloss = opt.inte_loss_weight[i] * content_criterion(coe*sr_var[i], coe*hr_var)
                else:
                    single_srloss = opt.inte_loss_weight[i] * GW_loss(sr_var[i], hr_var)
                sr_loss += single_srloss
                train_info += ' H%d: %.4f'
                train_info_tuple.append(i)
                train_info_tuple.append(single_srloss.item())
        else:
            sr_loss = content_criterion(sr_var, hr_var)
        loss += opt.sr_lambda * sr_loss
        train_info += ' total: %.4f'
        train_info_tuple.append(sr_loss.item())

        train_info += ' tot: %.4f'
        train_info_tuple.append(loss.item())

        scheduler.step()
        optim_.zero_grad()
        loss.backward()
        optim_.step()

        logger.print_log(train_info % tuple(train_info_tuple), with_time=False)
        iter_n = batch + epoch * len(loader)

    end = datetime.datetime.now()
    running_lr = scheduler.get_lr()
    logger.print_log(' epoch: [%d/%d] elapse: %s lr: %.6f'
                     % (epoch + 1, opt.epochs, str(end - start)[:-4], running_lr[0]))