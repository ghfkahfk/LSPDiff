import io
import random
import torch
from torch.utils.data import Dataset, DataLoader, ConcatDataset
from torch.utils.data import sampler
import torchvision.transforms as transforms
import lmdb
import six
import sys
import bisect
import warnings
from PIL import Image
import numpy as np
import string
import cv2
import os
import re
import imgaug.augmenters as iaa
from tqdm import tqdm
import sys
sys.path.append(r'D:\PycharmProjects\pythonProject\RGDiffSR-main')
from utils import utils_deblur
from utils import utils_sisr as sr
from einops import rearrange, repeat
from matplotlib import pyplot as plt

kernel = utils_deblur.fspecial('gaussian', 15, 1.)


def buf2PIL(txn, key, type='RGB'):
    imgbuf = txn.get(key)
    buf = six.BytesIO()
    buf.write(imgbuf)
    buf.seek(0)
    im = Image.open(buf).convert(type)
    return im


def gauss_unsharp_mask(rgb, shp_kernel, shp_sigma, shp_gain):
    LF = cv2.GaussianBlur(rgb, (shp_kernel, shp_kernel), shp_sigma)
    HF = rgb - LF
    RGB_peak = rgb + HF * shp_gain
    RGB_noise_NR_shp = np.clip(RGB_peak, 0.0, 255.0)
    return RGB_noise_NR_shp, LF


def add_shot_gauss_noise(rgb, shot_noise_mean, read_noise):
    noise_var_map = shot_noise_mean * rgb + read_noise
    noise_dev_map = np.sqrt(noise_var_map)
    noise = np.random.normal(loc=0.0, scale=noise_dev_map, size=None)
    if (rgb.mean() > 252.0):
        noise_rgb = rgb
    else:
        noise_rgb = rgb + noise
    noise_rgb = np.clip(noise_rgb, 0.0, 255.0)
    return noise_rgb


def degradation(src_img):
    # RGB Image input
    GT_RGB = np.array(src_img)
    GT_RGB = GT_RGB.astype(np.float32)

    pre_blur_kernel_set = [3, 5]
    sharp_kernel_set = [3, 5]
    blur_kernel_set = [5, 7, 9, 11]
    NR_kernel_set = [3, 5]

    # Pre Blur
    kernel = pre_blur_kernel_set[random.randint(0, (len(pre_blur_kernel_set) - 1))]
    blur_sigma = random.uniform(5., 6.)
    RGB_pre_blur = cv2.GaussianBlur(GT_RGB, (kernel, kernel), blur_sigma)

    rand_p = random.random()
    if rand_p > 0.2:
        # Noise
        shot_noise = random.uniform(0, 0.005)
        read_noise = random.uniform(0, 0.015)
        GT_RGB_noise = add_shot_gauss_noise(RGB_pre_blur, shot_noise, read_noise)
    else:
        GT_RGB_noise = RGB_pre_blur

    # Noise Reduction
    choice = random.uniform(0, 1.0)
    GT_RGB_noise = np.round(GT_RGB_noise)
    GT_RGB_noise = GT_RGB_noise.astype(np.uint8)
    # if (shot_noise < 0.06):
    if (choice < 0.7):
        NR_kernel = NR_kernel_set[random.randint(0, (len(NR_kernel_set) - 1))]  ###3,5,7,9
        NR_sigma = random.uniform(2., 3.)
        GT_RGB_noise_NR = cv2.GaussianBlur(GT_RGB_noise, (NR_kernel, NR_kernel), NR_sigma)
    else:
        value_sigma = random.uniform(70, 80)
        space_sigma = random.uniform(70, 80)
        GT_RGB_noise_NR = cv2.bilateralFilter(GT_RGB_noise, 7, value_sigma, space_sigma)

    # Sharpening
    GT_RGB_noise_NR = GT_RGB_noise_NR.astype(np.float32)
    shp_kernel = sharp_kernel_set[random.randint(0, (len(sharp_kernel_set) - 1))]  ###5,7,9
    shp_sigma = random.uniform(2., 3.)
    shp_gain = random.uniform(3., 4.)
    RGB_noise_NR_shp, LF = gauss_unsharp_mask(GT_RGB_noise_NR, shp_kernel, shp_sigma, shp_gain)

    # print("RGB_noise_NR_shp:", RGB_noise_NR_shp.shape)

    return Image.fromarray(RGB_noise_NR_shp.astype(np.uint8))


def str_filt(str_, voc_type):
    alpha_dict = {
        'digit': string.digits,
        'lower': string.digits + string.ascii_lowercase,
        'upper': string.digits + string.ascii_letters,
        'all': string.digits + string.ascii_letters + string.punctuation,
        'chinese': open("al_chinese.txt", "r").readlines()[0].replace("\n", "")
    }
    if voc_type == 'lower':
        str_ = str_.lower()

    if voc_type == 'chinese':  # Chinese character only
        new_str = ""
        for ch in str_:
            if '\u4e00' <= ch <= '\u9fa5' or ch in string.digits + string.ascii_letters:
                new_str += ch
        str_ = new_str
    for char in str_:
        if char not in alpha_dict[voc_type]:  # voc_type
            str_ = str_.replace(char, '')
    return str_


class lmdbDataset_real(Dataset):
    def __init__(
            self, root=None,
            voc_type='upper',
            max_len=100,
            test=False,
            cutblur=False,
            manmade_degrade=False,
            rotate=None,
            ocr_data=False
    ):
        super(lmdbDataset_real, self).__init__()
        self.env = lmdb.open(
            root,
            max_readers=1,
            readonly=True,
            lock=False,
            readahead=False,
            meminit=False)

        self.cb_flag = cutblur
        self.rotate = rotate

        if not self.env:
            print('cannot creat lmdb from %s' % (root))
            sys.exit(0)

        with self.env.begin(write=False) as txn:
            nSamples = int(txn.get(b'num-samples'))
            self.nSamples = nSamples
            print("nSamples:", nSamples)
        self.voc_type = voc_type
        self.max_len = max_len
        self.test = test

        self.manmade_degrade = manmade_degrade
        self.ocr_data = ocr_data

    def __len__(self):
        return self.nSamples

    def rotate_img(self, image, angle):
        # convert to cv2 image

        if not angle == 0.0:
            image = np.array(image)
            (h, w) = image.shape[:2]
            scale = 1.0
            # set the rotation center
            center = (w / 2, h / 2)
            # anti-clockwise angle in the function
            M = cv2.getRotationMatrix2D(center, angle, scale)
            image = cv2.warpAffine(image, M, (w, h))
            # back to PIL image
            image = Image.fromarray(image)

        return image

    def cutblur(self, img_hr, img_lr):
        p = random.random()

        img_hr_np = np.array(img_hr)
        img_lr_np = np.array(img_lr)

        randx = int(img_hr_np.shape[1] * (0.2 + 0.8 * random.random()))

        if p > 0.7:
            left_mix = random.random()
            if left_mix <= 0.5:
                img_lr_np[:, randx:] = img_hr_np[:, randx:]
            else:
                img_lr_np[:, :randx] = img_hr_np[:, :randx]

        return Image.fromarray(img_lr_np)

    def __getitem__(self, index):
        assert index <= len(self), 'index range error'
        index += 1
        txn = self.env.begin(write=False)
        label_key = b'label-%09d' % index
        word = "  "  # str(txn.get(label_key).decode())
        # print("in dataset....")
        img_HR_key = b'image_hr-%09d' % index  # 128*32
        img_lr_key = b'image_lr-%09d' % index  # 64*16

        if self.ocr_data:
            img_HR_key = img_lr_key = b'image-%09d' % index

        try:
            img_HR = buf2PIL(txn, img_HR_key, 'RGB')
            if self.manmade_degrade:
                img_lr = degradation(img_HR)
            else:
                img_lr = buf2PIL(txn, img_lr_key, 'RGB')
            # print("GOGOOGO..............", img_HR.size)
            if self.cb_flag and not self.test:
                img_lr = self.cutblur(img_HR, img_lr)

            if not self.rotate is None:

                if not self.test:
                    angle = random.random() * self.rotate * 2 - self.rotate
                else:
                    angle = 0  # self.rotate

                # img_HR = self.rotate_img(img_HR, angle)
                # img_lr = self.rotate_img(img_lr, angle)

            img_lr_np = np.array(img_lr).astype(np.uint8)
            img_lry = cv2.cvtColor(img_lr_np, cv2.COLOR_RGB2YUV)
            img_lry = Image.fromarray(img_lry)

            img_HR_np = np.array(img_HR).astype(np.uint8)
            img_HRy = cv2.cvtColor(img_HR_np, cv2.COLOR_RGB2YUV)
            img_HRy = Image.fromarray(img_HRy)
            word = txn.get(label_key)
            if word is None:
                print("None word:", label_key)
                word = " "
            else:
                word = str(word.decode())
            # print("img_HR:", img_HR.size, img_lr.size())

        except IOError or len(word) > self.max_len:
            return self[index + 1]
        label_str = str_filt(word, self.voc_type)

        return img_HR, img_lr, img_HRy, img_lry, label_str, index


class multi_lmdbDataset(ConcatDataset):
    def __init__(self, roots):
        datasets = []
        for path in roots:
            # print("##############################################",path)
            datasets.append(lmdbDataset_real(root=path, voc_type='all'))
        super(multi_lmdbDataset, self).__init__(datasets)


class resizeNormalize(object):
    def __init__(self, size, mask=False, interpolation=Image.BICUBIC, aug=None, blur=False):
        self.size = size
        self.interpolation = interpolation
        self.toTensor = transforms.ToTensor()
        self.mask = mask
        self.aug = aug

        self.blur = blur

    def __call__(self, img, ratio_keep=False):

        size = self.size

        if ratio_keep:
            ori_width, ori_height = img.size
            ratio = float(ori_width) / ori_height

            if ratio < 3:
                width = 100  # if self.size[0] == 32 else 50
            else:
                width = int(ratio * self.size[1])

            size = (width, self.size[1])

        # print("size:", size)
        img = img.resize(size, self.interpolation)

        if self.blur:
            # img_np = np.array(img)
            # img_np = cv2.GaussianBlur(img_np, (5, 5), 1)
            # print("in degrade:", np.unique(img_np))
            # img_np = noisy("gauss", img_np).astype(np.uint8)
            # img_np = apply_brightness_contrast(img_np, 40, 40).astype(np.uint8)
            # img_np = JPEG_compress(img_np)

            # img = Image.fromarray(img_np)
            pass

        if not self.aug is None:
            img_np = np.array(img)
            # print("imgaug_np:", imgaug_np.shape)
            imgaug_np = self.aug(images=img_np[None, ...])
            img = Image.fromarray(imgaug_np[0, ...])

        img_tensor = self.toTensor(img)
        if self.mask:
            mask = img.convert('L')
            thres = np.array(mask).mean()
            mask = mask.point(lambda x: 0 if x > thres else 255)
            mask = self.toTensor(mask)
            img_tensor = torch.cat((img_tensor, mask), 0)

        return img_tensor


class alignCollate_syn(object):
    def __init__(self, imgH=64,
                 imgW=256,
                 down_sample_scale=4,
                 keep_ratio=False,
                 min_ratio=1,
                 mask=False,
                 alphabet=53,
                 train=True,
                 y_domain=False
                 ):

        sometimes = lambda aug: iaa.Sometimes(0.2, aug)

        aug = [
            iaa.GaussianBlur(sigma=(0.0, 3.0)),
            iaa.AverageBlur(k=(1, 5)),
            iaa.MedianBlur(k=(3, 7)),
            iaa.BilateralBlur(
                d=(3, 9), sigma_color=(10, 250), sigma_space=(10, 250)),
            iaa.MotionBlur(k=3),
            iaa.MeanShiftBlur(),
            iaa.Superpixels(p_replace=(0.1, 0.5), n_segments=(1, 7))
        ]

        self.aug = iaa.Sequential([sometimes(a) for a in aug], random_order=True)

        # self.y_domain = y_domain

        self.imgH = imgH
        self.imgW = imgW
        self.keep_ratio = keep_ratio
        self.min_ratio = min_ratio
        self.down_sample_scale = down_sample_scale
        self.mask = mask
        # self.alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
        self.alphabet = open("al_chinese.txt", "r",encoding="utf-8").readlines()[0].replace("\n", "")
        self.d2a = "-" + self.alphabet
        self.alsize = len(self.d2a)
        self.a2d = {}
        cnt = 0
        for ch in self.d2a:
            self.a2d[ch] = cnt
            cnt += 1

        imgH = self.imgH
        imgW = self.imgW

        self.transform = resizeNormalize((imgW, imgH), self.mask)
        self.transform2 = resizeNormalize((imgW // self.down_sample_scale, imgH // self.down_sample_scale), self.mask,
                                          blur=True)
        self.transform_pseudoLR = resizeNormalize((imgW // self.down_sample_scale, imgH // self.down_sample_scale),
                                                  self.mask, aug=self.aug)

        self.train = train

    def degradation(self, img_L):
        # degradation process, blur + bicubic downsampling + Gaussian noise
        # if need_degradation:
        # img_L = util.modcrop(img_L, sf)
        img_L = np.array(img_L)
        # print("img_L_before:", img_L.shape, np.unique(img_L))
        img_L = sr.srmd_degradation(img_L, kernel)

        noise_level_img = 0.
        if not self.train:
            np.random.seed(seed=0)  # for reproducibility
        # print("unique:", np.unique(img_L))
        img_L = img_L + np.random.normal(0, noise_level_img, img_L.shape)

        # print("img_L_after:", img_L_beore.shape, img_L.shape, np.unique(img_L))

        return Image.fromarray(img_L.astype(np.uint8))

    def __call__(self, batch):
        images, images_lr, _, _, label_strs = zip(*batch)

        # [self.degradation(image) for image in images]
        # images_hr = images
        '''
        images_lr = [image.resize(
            (image.size[0] // self.down_sample_scale, image.size[1] // self.down_sample_scale),
            Image.BICUBIC) for image in images]

        if self.train:
            if random.random() > 1.5:
                images_hr = [image.resize(
                (image.size[0]//self.down_sample_scale, image.size[1]//self.down_sample_scale),
                Image.BICUBIC) for image in images]
            else:
                images_hr = images
        else:
            images_hr = images
            #[image.resize(
            #    (image.size[0] // self.down_sample_scale, image.size[1] // self.down_sample_scale),
            #    Image.BICUBIC) for image in images]
        '''
        # images_hr = [self.degradation(image) for image in images]
        images_hr = images
        # images_lr = [image.resize(
        #     (image.size[0] // 4, image.size[1] // 4),
        #     Image.BICUBIC) for image in images_lr]
        # images_lr = images

        # images_lr_new = []
        # for image in images_lr:
        #    image_np = np.array(image)
        #    image_aug = self.aug(images=image_np[None, ])[0]
        #    images_lr_new.append(Image.fromarray(image_aug))
        # images_lr = images_lr_new

        images_hr = [self.transform(image) for image in images_hr]
        images_hr = torch.cat([t.unsqueeze(0) for t in images_hr], 0)

        if self.train:
            images_lr = [image.resize(
                (image.size[0] // 2, image.size[1] // 2),  # self.down_sample_scale
                Image.BICUBIC) for image in images_lr]
        else:
            pass
        #    # for image in images_lr:
        #    #     print("images_lr:", image.size)
        #    images_lr = [image.resize(
        #         (image.size[0] // self.down_sample_scale, image.size[1] // self.down_sample_scale),  # self.down_sample_scale
        #        Image.BICUBIC) for image in images_lr]
        #    pass
        # images_lr = [self.degradation(image) for image in images]
        images_lr = [self.transform2(image) for image in images_lr]

        images_lr = torch.cat([t.unsqueeze(0) for t in images_lr], 0)

        max_len = 26

        label_batches = []
        weighted_tics = []
        weighted_masks = []

        for word in label_strs:
            word = word.lower()
            # Complement

            if len(word) > 4:
                word = [ch for ch in word]
                word[2] = "e"
                word = "".join(word)

            if len(word) <= 1:
                pass
            elif len(word) < 26 and len(word) > 1:
                # inter_com = 26 - len(word)
                # padding = int(inter_com / (len(word) - 1))
                # new_word = word[0]
                # for i in range(len(word) - 1):
                #    new_word += "-" * padding + word[i + 1]

                # word = new_word
                pass
            else:
                word = word[:26]

            label_list = [self.a2d[ch] for ch in word if ch in self.a2d]

            if len(label_list) <= 0:
                # blank label
                weighted_masks.append(0)
            else:
                weighted_masks.extend(label_list)

            labels = torch.tensor(label_list)[:, None].long()
            label_vecs = torch.zeros((labels.shape[0], self.alsize))
            # print("labels:", labels)
            # if labels.shape[0] > 0:
            #    label_batches.append(label_vecs.scatter_(-1, labels, 1))
            # else:
            #    label_batches.append(label_vecs)

            if labels.shape[0] > 0:
                label_vecs = torch.zeros((labels.shape[0], self.alsize))
                label_batches.append(label_vecs.scatter_(-1, labels, 1))
                weighted_tics.append(1)
            else:
                label_vecs = torch.zeros((1, self.alsize))
                label_vecs[0, 0] = 1.
                label_batches.append(label_vecs)
                weighted_tics.append(0)

        label_rebatches = torch.zeros((len(label_strs), max_len, self.alsize))

        for idx in range(len(label_strs)):
            label_rebatches[idx][:label_batches[idx].shape[0]] = label_batches[idx]

        label_rebatches = label_rebatches.unsqueeze(1).float().permute(0, 3, 1, 2)

        # print(images_lr.shape, images_hr.shape)

        return images_hr, images_lr, images_hr, images_lr, label_strs, label_rebatches, torch.tensor(
            weighted_masks).long(), torch.tensor(weighted_tics)


class alignCollate_realWTL(alignCollate_syn):
    def __call__(self, batch):
        images_HR, images_lr, images_HRy, images_lry, label_strs, indexes= zip(*batch)
        imgH = self.imgH
        imgW = self.imgW
        # transform = resizeNormalize((imgW, imgH), self.mask)
        # transform2 = resizeNormalize((imgW // self.down_sample_scale, imgH // self.down_sample_scale), self.mask)
        images_HR = [self.transform(image) for image in images_HR]
        images_HR = torch.cat([t.unsqueeze(0) for t in images_HR], 0)

        images_lr = [self.transform2(image) for image in images_lr]
        images_lr = torch.cat([t.unsqueeze(0) for t in images_lr], 0)

        images_lry = [self.transform2(image) for image in images_lry]
        images_lry = torch.cat([t.unsqueeze(0) for t in images_lry], 0)

        images_HRy = [self.transform(image) for image in images_HRy]
        images_HRy = torch.cat([t.unsqueeze(0) for t in images_HRy], 0)

        max_len = 26

        label_batches = []

        for word in label_strs:
            word = word.lower()
            # Complement

            if len(word) > 4:
                word = [ch for ch in word]
                word[2] = "e"
                word = "".join(word)

            if len(word) <= 1:
                pass
            elif len(word) < 26 and len(word) > 1:
                inter_com = 26 - len(word)
                padding = int(inter_com / (len(word) - 1))
                new_word = word[0]
                for i in range(len(word) - 1):
                    new_word += "-" * padding + word[i + 1]

                word = new_word
                pass
            else:
                word = word[:26]

            label_list = [self.a2d[ch] for ch in word if ch in self.a2d]

            labels = torch.tensor(label_list)[:, None].long()
            label_vecs = torch.zeros((labels.shape[0], self.alsize))
            # print("labels:", labels)
            if labels.shape[0] > 0:
                label_batches.append(label_vecs.scatter_(-1, labels, 1))
            else:
                label_batches.append(label_vecs)
        label_rebatches = torch.zeros((len(label_strs), max_len, self.alsize))

        for idx in range(len(label_strs)):
            label_rebatches[idx][:label_batches[idx].shape[0]] = label_batches[idx]

        label_rebatches = label_rebatches.unsqueeze(1).float().permute(0, 3, 1, 2)

        images_HR = rearrange(images_HR, 'b c h w -> b h w c')
        images_lr = rearrange(images_lr, 'b c h w -> b h w c')

        example = {'image': images_HR, 'LR_image': images_lr, 'label': label_strs, 'id':indexes}
        return example

        # return images_HR, images_lr, images_HRy, images_lry, label_strs, label_rebatches


class alignCollate_realWTL_forVQGAN(alignCollate_syn):
    def __call__(self, batch):
        images_HR, images_lr, images_HRy, images_lry, label_strs = zip(*batch)
        imgH = self.imgH
        imgW = self.imgW
        # transform = resizeNormalize((imgW, imgH), self.mask)
        # transform2 = resizeNormalize((imgW // self.down_sample_scale, imgH // self.down_sample_scale), self.mask)
        images_HR = [self.transform(image) for image in images_HR]
        images_HR = torch.cat([t.unsqueeze(0) for t in images_HR], 0)

        images_HR = rearrange(images_HR, 'b c h w -> b h w c')

        example = {'image': images_HR}
        return example


def sample(path, ocr_data, n):
    if ocr_data:
        txzm = lmdbDataset_real(root='/data2/zhouyuxuan/ocr_data/' + path, voc_type='all', ocr_data=True)
    else:
        txzm = lmdbDataset_real(root='E:/Dataset/text_data/textzoom/' + path, voc_type='all')
    items = []
    idx = []
    os.makedirs(f'imgs/{path}/', exist_ok=True)
    for i in range(n):
        id = random.randint(0, txzm.__len__())
        img_HR, img_lr, img_HRy, img_lry, label_str = txzm.__getitem__(id)
        items.append(txzm.__getitem__(id))
        idx.append(id)
        print(img_HR.size, img_lr.size, label_str)
        img_HR.save(f'imgs/{path}/{id}HR.jpg')

    collate_fn = alignCollate_realWTL(imgH=64, imgW=256, down_sample_scale=4, mask=False, train=True)

    resize_res = collate_fn(items)
    # print(resize_res)
    image = resize_res['image']
    LR_image = resize_res['LR_image']
    label = resize_res['label']
    os.makedirs(f'r_imgs/{path}/', exist_ok=True)
    for i in range(n):
        img = np.array(image[i])
        img -= np.min(img)
        img /= np.max(img)
        img = img * 255
        img = Image.fromarray(img.astype('uint8'))
        img.save(f'r_imgs/{path}/{idx[i]}HR.jpg')

        img = np.array(LR_image[i])
        img -= np.min(img)
        img /= np.max(img)
        img = img * 255
        img = Image.fromarray(img.astype('uint8'))
        img.save(f'r_imgs/{path}/{idx[i]}LR.jpg')

        print(image[i].shape, LR_image[i].shape, label[i])


def count(path, ocr_data):
    os.makedirs('statistics/', exist_ok=True)
    if ocr_data:
        txzm = lmdbDataset_real(root='/data2/zhouyuxuan/ocr_data/' + path, voc_type='all', ocr_data=True)
    else:
        txzm = lmdbDataset_real(root='E:/Dataset/text_data/textzoom/' + path, voc_type='all')
    H = []
    R = []
    for i in tqdm(range(txzm.__len__())):
        img_HR, img_lr, img_HRy, img_lry, label_str = txzm.__getitem__(i)
        width, height = img_HR.size
        ratio = width / height
        if height < 200:
            H.append(height)
        R.append(ratio)

    plt.hist(H, bins=50)
    plt.savefig(f'statistics/{path}_heights.png')
    plt.cla()
    plt.hist(R, bins=50)
    plt.savefig(f'statistics/{path}_ratio.png')
    plt.cla()


def count2(path, ocr_data):
    if ocr_data:
        txzm = lmdbDataset_real(root='/data2/zhouyuxuan/ocr_data/' + path, voc_type='all', ocr_data=True)
    else:
        txzm = lmdbDataset_real(root='E:/Dataset/text_data/textzoom/' + path, voc_type='all')
    cnt = 0
    for i in tqdm(range(txzm.__len__())):
        img_HR, img_lr, img_HRy, img_lry, label_str = txzm.__getitem__(i)
        width, height = img_HR.size
        ratio = width / height
        if height >= 25 and height <= 200 and ratio >= 1 and ratio <= 4:
            cnt += 1

    print(path, cnt)
    return cnt


def write_cache(env, cache):
    txn = env.begin(write=True)
    for k, v in cache.items():
        txn.put(k, v)
    txn.commit()


def select(path, n=None):
    old_env = lmdb.open(
        '/data2/zhouyuxuan/ocr_data/' + path,
        max_readers=1,
        readonly=True,
        lock=False,
        readahead=False,
        meminit=False)

    if not old_env:
        print('cannot creat lmdb from %s' % (path))
        sys.exit(0)

    with old_env.begin(write=False) as txn:
        nSamples = int(txn.get(b'num-samples'))
        print(f"{path} nSamples:", nSamples)

    idx = list(range(nSamples))
    random.shuffle(idx)
    if n is None:
        n = nSamples
    txn = old_env.begin(write=False)

    sr_path = '/data2/zhouyuxuan/sr_data/' + path
    os.makedirs(sr_path, exist_ok=True)
    env = lmdb.open(sr_path, map_size=1099511627776)
    cache = {}
    valid_num = 0

    for i in tqdm(range(nSamples)):

        label_key = b'label-%09d' % idx[i]
        img_key = b'image-%09d' % idx[i]  # 128*32

        try:
            img = buf2PIL(txn, img_key, 'RGB')
            width, height = img.size
            ratio = width / height
            if height > 200 or height < 25 or ratio < 0.8 or ratio > 4:
                continue

            word = txn.get(label_key)
            if word is None:
                continue
            else:
                word = str(word.decode())
            word = str_filt(word, 'all')
            if len(word) > 100 or len(word) < 1:
                continue

            valid_num += 1
            buff = io.BytesIO()
            img.save(buff, format='PNG')
            image_lr_key = 'image_lr-%09d'.encode() % valid_num
            image_hr_key = 'image_hr-%09d'.encode() % valid_num
            label_key = 'label-%09d'.encode() % valid_num
            cache[image_lr_key] = buff.getvalue()
            cache[image_hr_key] = buff.getvalue()
            cache[label_key] = word.encode()

            if valid_num % 1000 == 0:
                write_cache(env, cache)
                cache = {}
                # print('Written %d / %d' % (valid_num, output_size))
            if valid_num >= n:
                break
        except Exception:
            print(f'error at {path} {idx}')
            continue

    n_samples = valid_num
    cache['num-samples'.encode()] = str(n_samples).encode()
    write_cache(env, cache)
    print('Created dataset %s with %d samples' % (path, n_samples))


def toMask(img_tensor):
    unloader = transforms.ToPILImage()
    toTensor = transforms.ToTensor()
    # mask = unloader(img_tensor).convert('L')
    mask=img_tensor.convert('L')
    thres = np.array(mask).mean()
    mask = mask.point(lambda x: 0 if x > thres else 255)
    # mask = toTensor(mask).unsqueeze(0)
    # mask = mask.repeat(1, 3, 1, 1)
    return mask

if __name__ == '__main__':

    txzm = lmdbDataset_real(root='E:/Dataset/text_data/textzoom/test/easy' , voc_type='all')
    img_HR, img_lr, img_HRy, img_lry, label_str = txzm.__getitem__(21)
    img_lr = img_lr.resize((128, 32))
    img_HR = img_HR.resize((128, 32))
    mask=toMask(img_lr)
    print(mask)
    img_lr.save('E:/Dataset/text_data/a.png')
    mask.save('E:/Dataset/text_data/b.png')
    img_HR.save('E:/Dataset/text_data/c.png')


    exit(0)

    for dataset in ['IIIT5K', 'COCO_Text', 'ICDAR2013', 'ICDAR2015']:
        select(dataset)
    select('synth90K_shuffle', 50000)
    for dataset in ['SynthAdd',
                    'SynthText800K_shuffle_1_40',
                    'SynthText800K_shuffle_41_80',
                    'SynthText800K_shuffle_81_160',
                    'SynthText800K_shuffle_161_200']:
        select(dataset, 30000)
    exit(0)
    count('train1', False)
    count('train2', False)
    for dataset in ['IIIT5K', 'COCO_Text', 'ICDAR2013', 'ICDAR2015',
                    'synth90K_shuffle', 'SynthAdd',
                    'SynthText800K_shuffle_1_40',
                    'SynthText800K_shuffle_41_80',
                    'SynthText800K_shuffle_81_160',
                    'SynthText800K_shuffle_161_200']:
        count(dataset, True)


