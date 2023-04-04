import dataset_loader
from braindecode import EEGClassifier
from braindecode.util import set_random_seeds
import torch
from torch import nn, optim
from braindecode.augmentation import AugmentedDataLoader, SignFlip, FrequencyShift
from skorch.helper import predefined_split, SliceDataset
from skorch.callbacks import LRScheduler
from sklearn.model_selection import KFold, cross_val_score
import numpy as np
import moabb
from moabb.evaluations import CrossSessionEvaluation, WithinSessionEvaluation, CrossSubjectEvaluation
from moabb.paradigms import MotorImagery
from mne.decoding import CSP
from pyriemann.estimation import Covariances
from pyriemann.tangentspace import TangentSpace
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis as LDA
from sklearn.linear_model import LogisticRegression
from sklearn.svm import SVC
from sklearn.pipeline import make_pipeline
from utils import get_augmentation_transform
import nn_models
from nn_models import cuda
import pandas as pd
from sklearn.model_selection import GridSearchCV
from braindecode.preprocessing import exponential_moving_standardize, preprocess, Preprocessor
from numpy import multiply
import time
from datetime import datetime
from braindecode.models import to_dense_prediction_model, get_output_shape
from braindecode.training import CroppedLoss
from torchinfo import summary
from torch.utils.data import ConcatDataset, DataLoader

moabb.set_log_level("info")


def _cross_subject_experiment(model_name, windows_dataset, clf, n_epochs):
    # for every subject in dataset, fit classifier and test
    split_by_subject = windows_dataset.split('subject')
    train_subjects = ['9', '2', '3', '4', '5', '6', '7', '8']
    test_subjects = ['1']
    train_set = ConcatDataset([split_by_subject[i] for i in train_subjects])
    test_set = ConcatDataset([split_by_subject[i] for i in test_subjects])
    clf.train_split = predefined_split(test_set)
    clf.fit(train_set, y=None, epochs=n_epochs)


def bci2a_eeg_net():
    set_random_seeds(seed=14388341, cuda=cuda)
    ds = dataset_loader.DatasetFromBraindecode('bci2a', subject_ids=None)
    ds.preprocess_dataset()
    # ds.preprocess_dataset(resample_freq=128)
    # windows_dataset = ds.create_windows_dataset(trial_start_offset_seconds=0.5, trial_stop_offset_seconds=-1.5)
    windows_dataset = ds.create_windows_dataset(trial_start_offset_seconds=-0.5)
    n_channels = ds.get_channel_num()
    input_window_samples = ds.get_input_window_sample()
    model = nn_models.EEGNetv4(in_chans=n_channels, n_classes=4, input_window_samples=input_window_samples,
                               kernel_length=64, drop_prob=0.5)
    # model = nn_models.ST_GCN(n_channels=n_channels, n_classes=4, input_window_size=input_window_samples,
    #                          kernel_length=64)
    # model = nn_models.EEGNetRp(n_channels=n_channels, n_classes=4, input_window_size=input_window_samples,
    #                            kernel_length=64, drop_p=0.5)
    # model = nn_models.ASTGCN(n_channels=n_channels, n_classes=4, input_window_size=input_window_samples,
    #                          kernel_length=32)
    # model = nn_models.EEGNetGCN(n_channels=n_channels, n_classes=4, input_window_size=input_window_samples,
    #                             kernel_length=64)
    # model = nn_models.GCNEEGNet(n_channels=n_channels, n_classes=4, input_window_size=input_window_samples,
    #                             kernel_length=64)
    if cuda:
        model.cuda()
    summary(model, (1, n_channels, input_window_samples, 1))
    n_epochs = 750
    lr = 0.001
    weight_decay = 0
    batch_size = 64
    clf = EEGClassifier(module=model,
                        criterion=torch.nn.CrossEntropyLoss, optimizer=torch.optim.AdamW, train_split=None,
                        optimizer__lr=lr, optimizer__weight_decay=weight_decay, batch_size=batch_size,
                        callbacks=["accuracy", ("lr_scheduler", LRScheduler('CosineAnnealingLR', T_max=n_epochs - 1))],
                        device='cuda' if cuda else 'cpu'
                        )
    # _within_subject_experiment(model_name='EEGNet', windows_dataset=windows_dataset, clf=clf, n_epochs=n_epochs)
    _cross_subject_experiment(model_name='EEGNet', windows_dataset=windows_dataset, clf=clf, n_epochs=n_epochs)


def bci2a_eeg_net_t():
    set_random_seeds(seed=14388341, cuda=cuda)
    ds = dataset_loader.DatasetFromBraindecode('bci2a', subject_ids=[1])
    ds.preprocess_dataset(resample_freq=128)
    windows_dataset = ds.create_windows_dataset(trial_start_offset_seconds=-0.5)
    n_channels = ds.get_channel_num()
    input_window_samples = ds.get_input_window_sample()
    model = nn_models.EEGNetv4(in_chans=n_channels, n_classes=4, input_window_samples=input_window_samples,
                               kernel_length=64, drop_prob=0.5)
    if cuda:
        model.cuda()
    summary(model, (1, n_channels, input_window_samples, 1))
    n_epochs = 100
    lr = 0.001
    batch_size = 64
    subjects_windows_dataset = windows_dataset.split('subject')
    subjects_accuracy = []
    for subject, windows_dataset in subjects_windows_dataset.items():
        model = nn_models.EEGNetv4(in_chans=n_channels, n_classes=4, input_window_samples=input_window_samples,
                                   kernel_length=64, drop_prob=0.5)
        if cuda:
            model.cuda()
        split_by_session = windows_dataset.split('session')
        train_set = split_by_session['session_T']
        test_set = split_by_session['session_E']
        train_dataloader = DataLoader(train_set, batch_size=batch_size, shuffle=True)
        test_dataloader = DataLoader(test_set, batch_size=batch_size)
        loss_function = nn.NLLLoss()
        optimizer = optim.Adam(model.parameters(), lr=lr)
        scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer=optimizer, T_max=n_epochs - 1)
        for epoch in range(n_epochs):
            train_loss = 0.
            train_accuracy = 0.
            model.train()
            for batch in train_dataloader:
                x_train, y_train, _ = batch
                if cuda:
                    x_train = x_train.cuda()
                    y_train = y_train.cuda()
                out = model(x_train)
                loss = loss_function(out, y_train)
                train_loss += loss
                predict = torch.max(out, 1)[1]
                accuracy = (predict == y_train).sum()
                train_accuracy += accuracy
                loss.backward()
                optimizer.step()
                optimizer.zero_grad()
            scheduler.step()
            train_loss = train_loss / len(train_dataloader)
            train_accuracy = train_accuracy / len(train_dataloader)

            valid_loss = 0.
            valid_accuracy = 0.
            model.eval()
            for batch in test_dataloader:
                x_test, y_test, _ = batch
                if cuda:
                    x_test = x_test.cuda()
                    y_test = y_test.cuda()
                out = model(x_test)
                loss = loss_function(out, y_test)
                valid_loss += loss
                predict = torch.max(out, 1)[1]
                accuracy = (predict == y_test).sum()
                valid_accuracy += accuracy
            valid_loss = valid_loss / len(test_dataloader)
            valid_accuracy = valid_accuracy / len(test_dataloader)
            print('Epoch: {} Train Loss: {:.6f} Acc: {:.6f} Valid Loss: {:.6f} Acc: {:.6f}'.format(
                epoch+1, train_loss, train_accuracy, valid_loss, valid_accuracy))


def _within_subject_experiment(model_name, windows_dataset, clf, n_epochs):
    # for every subject in dataset, fit classifier and test
    subjects_windows_dataset = windows_dataset.split('subject')
    subjects_accuracy = []
    for subject, windows_dataset in subjects_windows_dataset.items():
        split_by_session = windows_dataset.split('session')
        train_set = split_by_session['session_T']
        test_set = split_by_session['session_E']
        clf.train_split = predefined_split(test_set)
        clf.fit(train_set, y=None, epochs=n_epochs)


def bci2a_shallow_conv_net():
    set_random_seeds(seed=20233202, cuda=cuda)
    ds = dataset_loader.DatasetFromBraindecode('bci2a', subject_ids=None)
    ds.preprocess_dataset(low_freq=4, high_freq=38)
    n_channels = ds.get_channel_num()
    input_window_samples = 1000
    model = nn_models.ShallowFBCSPNet(in_chans=n_channels, n_classes=4, input_window_samples=input_window_samples,
                                      final_conv_length=30, drop_prob=0.25)
    if cuda:
        model.cuda()
    summary(model, (1, n_channels, input_window_samples, 1))
    # for cropped training
    to_dense_prediction_model(model)
    n_preds_per_input = get_output_shape(model, n_channels, input_window_samples)[2]
    windows_dataset = ds.create_windows_dataset(trial_start_offset_seconds=-0.5,
                                                window_size_samples=input_window_samples,
                                                window_stride_samples=n_preds_per_input)
    transforms = get_augmentation_transform(sample_freq=ds.get_sample_freq())
    n_epochs = 300
    lr = 0.000625
    weight_decay = 0
    batch_size = 64
    clf = EEGClassifier(module=model, iterator_train=AugmentedDataLoader, iterator_train__transforms=transforms,
                        train_split=None, criterion=CroppedLoss, criterion__loss_function=torch.nn.functional.nll_loss,
                        optimizer=torch.optim.AdamW, optimizer__lr=lr,
                        optimizer__weight_decay=weight_decay, batch_size=batch_size,
                        callbacks=["accuracy", ("lr_scheduler", LRScheduler('CosineAnnealingLR', T_max=n_epochs - 1))],
                        cropped=True, device='cuda' if cuda else 'cpu'
                        )
    _within_subject_experiment(model_name='ShallowConvNet', windows_dataset=windows_dataset, clf=clf, n_epochs=n_epochs)


def bci2a_csp_lda():
    ds = dataset_loader.DatasetFromMoabb('bci2a')
    datasets = [ds.dataset_instance]
    pipelines = {"CSP+LDA": make_pipeline(CSP(n_components=8), LDA())}
    paradigm = MotorImagery(n_classes=4)
    overwrite = False
    evaluation = CrossSessionEvaluation(
        paradigm=paradigm, datasets=datasets, suffix="examples", overwrite=overwrite
    )
    results = evaluation.process(pipelines)
    print(results)
