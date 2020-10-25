"""Code adapted from https://github.com/mseitzer/pytorch-fid
"""
import torch
import numpy as np
from scipy import linalg
from torch.utils.data import DataLoader
from torchvision import datasets, transforms
import argparse

import mnist_model
from mnist_generator import ConvDataGenerator, FCDataGenerator
from mnist_imputer import ComplementImputer, MaskImputer, FixedNoiseDimImputer
from masked_mnist import IndepMaskedMNIST, BlockMaskedMNIST
from pathlib import Path


use_cuda = torch.cuda.is_available()
device = torch.device('cuda' if use_cuda else 'cpu')

feature_layer = 0


def get_activations(image_generator, images, model, verbose=False):
    """Calculates the activations of the pool_3 layer for all images.

    Params:
    -- image_generator
                   : A generator that generates a batch of images at a time.
    -- images      : Number of images that will be generated by
                     image_generator.
    -- model       : Instance of inception model
    -- verbose     : If set to True and parameter out_step is given, the number
                     of calculated batches is reported.
    Returns:
    -- A numpy array of dimension (num images, dims) that contains the
       activations of the given tensor when feeding inception with the
       query tensor.
    """
    model.eval()

    pred_arr = None
    end = 0
    for i, batch in enumerate(image_generator):
        if verbose:
            print('\rPropagating batch %d' % (i + 1), end='', flush=True)
        start = end
        batch_size = batch.shape[0]
        end = start + batch_size
        batch = batch.to(device)

        with torch.no_grad():
            model(batch)
            pred = model.feature[feature_layer]
            batch_feature = pred.cpu().numpy().reshape(batch_size, -1)
            if pred_arr is None:
                pred_arr = np.empty((images, batch_feature.shape[1]))
            pred_arr[start:end] = batch_feature

    if verbose:
        print(' done')

    return pred_arr


def calculate_frechet_distance(mu1, sigma1, mu2, sigma2, eps=1e-6):
    """Numpy implementation of the Frechet Distance.
    The Frechet distance between two multivariate Gaussians X_1 ~ N(mu_1, C_1)
    and X_2 ~ N(mu_2, C_2) is
            d^2 = ||mu_1 - mu_2||^2 + Tr(C_1 + C_2 - 2*sqrt(C_1*C_2)).

    Stable version by Dougal J. Sutherland.

    Params:
    -- mu1   : Numpy array containing the activations of a layer of the
               inception net (like returned by the function 'get_predictions')
               for generated samples.
    -- mu2   : The sample mean over activations, precalculated on an
               representive data set.
    -- sigma1: The covariance matrix over activations for generated samples.
    -- sigma2: The covariance matrix over activations, precalculated on an
               representive data set.

    Returns:
    --   : The Frechet Distance.
    """

    mu1 = np.atleast_1d(mu1)
    mu2 = np.atleast_1d(mu2)

    sigma1 = np.atleast_2d(sigma1)
    sigma2 = np.atleast_2d(sigma2)

    assert mu1.shape == mu2.shape, \
        'Training and test mean vectors have different lengths'
    assert sigma1.shape == sigma2.shape, \
        'Training and test covariances have different dimensions'

    diff = mu1 - mu2

    # Product might be almost singular
    covmean, _ = linalg.sqrtm(sigma1.dot(sigma2), disp=False)
    if not np.isfinite(covmean).all():
        msg = ('fid calculation produces singular product; '
               'adding %s to diagonal of cov estimates') % eps
        print(msg)
        offset = np.eye(sigma1.shape[0]) * eps
        covmean = linalg.sqrtm((sigma1 + offset).dot(sigma2 + offset))

    # Numerical error might give slight imaginary component
    if np.iscomplexobj(covmean):
        if not np.allclose(np.diagonal(covmean).imag, 0, atol=1e-3):
            m = np.max(np.abs(covmean.imag))
            raise ValueError(f'Imaginary component {m}')
        covmean = covmean.real

    tr_covmean = np.trace(covmean)

    return (diff.dot(diff) + np.trace(sigma1) +
            np.trace(sigma2) - 2 * tr_covmean)


def calculate_activation_statistics(image_generator, images, model,
                                    verbose=False, weight=None):
    """Calculation of the statistics used by the FID.
    Params:
    -- image_generator
                   : A generator that generates a batch of images at a time.
    -- images      : Number of images that will be generated by
                     image_generator.
    -- model       : Instance of inception model
    -- verbose     : If set to True and parameter out_step is given, the
                     number of calculated batches is reported.
    Returns:
    -- mu    : The mean over samples of the activations of the pool_3 layer of
               the inception model.
    -- sigma : The covariance matrix of the activations of the pool_3 layer of
               the inception model.
    """
    act = get_activations(image_generator, images, model, verbose)
    if weight is None:
        mu = np.mean(act, axis=0)
        sigma = np.cov(act, rowvar=False)
    else:
        mu = np.average(act, axis=0, weights=weight)
        sigma = np.cov(act, rowvar=False, aweights=weight)
    return mu, sigma


class MNISTModel:
    def __init__(self):
        model = mnist_model.Net().to(device)
        model.eval()
        map_location = None if use_cuda else 'cpu'
        model.load_state_dict(
            torch.load('mnist.pth', map_location=map_location))

        stats_file = f'mnist_act_{feature_layer}.npz'
        try:
            f = np.load(stats_file)
            m_mnist, s_mnist = f['mu'][:], f['sigma'][:]
            f.close()
        except FileNotFoundError:
            data = datasets.MNIST('data', train=True, download=True,
                                  transform=transforms.ToTensor())
            images = len(data)
            batch_size = 64
            data_loader = DataLoader([image for image, _ in data],
                                     batch_size=batch_size)
            m_mnist, s_mnist = calculate_activation_statistics(
                data_loader, images, model, verbose=True)
            np.savez(stats_file, mu=m_mnist, sigma=s_mnist)

        self.model = model
        self.mnist_stats = m_mnist, s_mnist

    def get_feature(self, samples):
        self.model(samples)
        feature = self.model.feature[feature_layer]
        return feature.cpu().numpy().reshape(samples.shape[0], -1)

    def fid(self, features):
        mu = np.mean(features, axis=0)
        sigma = np.cov(features, rowvar=False)
        return calculate_frechet_distance(mu, sigma, *self.mnist_stats)


def data_generator_fid(data_generator,
                       n_samples=60000, batch_size=64, verbose=False):
    mnist_model = MNISTModel()
    latent_size = 128
    data_noise = torch.FloatTensor(batch_size, latent_size).to(device)

    with torch.no_grad():
        count = 0
        features = None
        while count < n_samples:
            data_noise.normal_()
            samples = data_generator(data_noise)
            batch_feature = mnist_model.get_feature(samples)

            if features is None:
                features = np.empty((n_samples, batch_feature.shape[1]))

            if count + batch_size > n_samples:
                batch_size = n_samples - count
                features[count:] = batch_feature[:batch_size]
            else:
                features[count:(count + batch_size)] = batch_feature

            count += batch_size
            if verbose:
                print(f'\rGenerate images {count}', end='', flush=True)
        if verbose:
            print(' done')
    return mnist_model.fid(features)


def imputer_fid(imputer, data, batch_size=64, verbose=False):
    mnist_model = MNISTModel()
    impu_noise = torch.FloatTensor(batch_size, 1, 28, 28).to(device)
    data_loader = DataLoader(data, batch_size=batch_size, drop_last=True)
    n_samples = len(data_loader) * batch_size

    with torch.no_grad():
        start = 0
        features = None
        for real_data, real_mask, _, index in data_loader:
            real_mask = real_mask.float()[:, None]
            real_data = real_data.to(device)
            real_mask = real_mask.to(device)
            impu_noise.uniform_()
            imputed_data = imputer(real_data, real_mask, impu_noise)

            batch_feature = mnist_model.get_feature(imputed_data)
            if features is None:
                features = np.empty((n_samples, batch_feature.shape[1]))
            features[start:(start + batch_size)] = batch_feature
            start += batch_size
            if verbose:
                print(f'\rGenerate images {start}', end='', flush=True)
        if verbose:
            print(' done')
    return mnist_model.fid(features)


def pretrained_misgan_fid(model_file, samples=60000, batch_size=64):
    model = torch.load(model_file, map_location='cpu')
    args = model['args']
    if args.generator == 'conv':
        DataGenerator = ConvDataGenerator
    elif args.generator == 'fc':
        DataGenerator = FCDataGenerator
    data_gen = DataGenerator().to(device)
    data_gen.load_state_dict(model['data_gen'])
    return data_generator_fid(data_gen, verbose=True)


def pretrained_imputer_fid(model_file, save_file, batch_size=64):
    model = torch.load(model_file, map_location='cpu')
    if 'imputer' not in model:
        return
    args = model['args']

    if args.imputer == 'comp':
        Imputer = ComplementImputer
    elif args.imputer == 'mask':
        Imputer = MaskImputer
    elif args.imputer == 'fix':
        Imputer = FixedNoiseDimImputer

    hid_lens = [int(n) for n in args.arch.split('-')]
    imputer = Imputer(arch=hid_lens).to(device)
    imputer.load_state_dict(model['imputer'])

    block_len = args.block_len
    if block_len == 0:
        block_len = None

    if args.mask == 'indep':
        data = IndepMaskedMNIST(obs_prob=args.obs_prob,
                                obs_prob_high=args.obs_prob_high)
    elif args.mask == 'block':
        data = BlockMaskedMNIST(block_len=block_len)

    fid = imputer_fid(imputer, data, verbose=True)
    with save_file.open('w') as f:
        print(fid, file=f)
    print('imputer fid:', fid)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root_dir')
    parser.add_argument('--skip-exist', action='store_true')
    args = parser.parse_args()

    skip_exist = args.skip_exist

    root_dir = Path(args.root_dir)
    fid_file = root_dir / f'fid-{feature_layer}.txt'
    if skip_exist and fid_file.exists():
        return
    try:
        model_file = max((root_dir / 'model').glob('*.pth'))
    except ValueError:
        return

    fid = pretrained_misgan_fid(model_file)
    print(f'{root_dir.name}: {fid}')
    with fid_file.open('w') as f:
        print(fid, file=f)

    # Compute FID for the imputer if it is in the model
    imputer_fid_file = root_dir / f'impute-fid-{feature_layer}.txt'
    pretrained_imputer_fid(model_file, imputer_fid_file)


if __name__ == '__main__':
    main()