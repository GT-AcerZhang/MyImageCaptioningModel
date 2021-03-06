import numpy as np
import os
import random
import time
from paddle import fluid

import config
import evaluate
from tools import util
from tools.logger import Logger
from model.model_adaAttention_aic import ImageCaptionModel
from reader import DataReader

seed = config.train['seed']
decoder_config = config.md['decoder']
encoder_config = config.md['encoder']
batch_size = config.train['batch_size']
capacity = config.train['data_loader_capacity']

logger = Logger()
data_reader = DataReader()
random.seed(seed)
np.random.seed(seed)


def get_optimizer():
    base_lr = config.train['learning_rate']
    strategy = config.train['lr_decay_strategy']
    lr = util.get_lr(strategy, base_lr, config.data['sample_count'], config.train['batch_size'])

    return fluid.optimizer.Adam(lr), lr


def training_net():
    startup_prog, train_prog = fluid.Program(), fluid.Program()
    train_prog.random_seed = 0  # 必须是0，否则dropout会出问题
    with fluid.program_guard(train_prog, startup_prog):
        with fluid.unique_name.guard():
            model = ImageCaptionModel()
            inputs, feed_list = model.build_input('train')
            loss = model.build_network('train', **inputs)
            if config.train['gradient_clip']:
                fluid.clip.set_gradient_clip(fluid.clip.GradientClipByValue(config.train['gradient_clip']))
            opt, lr = get_optimizer()
            opt.minimize(loss)
            loader = fluid.io.DataLoader.from_generator(feed_list=feed_list, capacity=capacity)
    return loss, lr, loader, startup_prog, train_prog


def eval_net():
    startup_prog, eval_prog = fluid.Program(), fluid.Program()
    with fluid.program_guard(eval_prog, startup_prog):
        with fluid.unique_name.guard():
            model = ImageCaptionModel()
            inputs, feed_list = model.build_input('eval')
            caption = model.build_network('eval', **inputs)

    return caption, startup_prog, eval_prog


def init_data_loader(loader, places, mode='train'):
    if mode not in ['train', 'dev']:
        raise ValueError('DataLoader不支持 {} 模式'.format(mode))
    rd = data_reader.get_reader(batch_size, mode)
    loader.set_sample_list_generator(rd, places)


def save_model(exe, train_program, eval_program, epoch, eval_prog_target=None, eval_bleu=None):
    if eval_prog_target is not None and not isinstance(eval_prog_target, list):
        raise ValueError('eval_prog_target 应当为 None 或 List类型')

    p = config.train['checkpoint_path']
    fluid.io.save_persistables(exe, os.path.join(p, 'checkpoint'), train_program)
    n = config.train['checkpoint_backup_every_n_epoch']
    if n and epoch % n == 0:
        fluid.io.save_persistables(exe, os.path.join(p, 'checkpoint{}'.format(epoch)), train_program)

    if config.train['export_params']:
        fluid.io.save_params(exe, os.path.join(p, 'params'), train_program)

    if config.train['export_infer_model'] and eval_prog_target is not None:
        fluid.io.save_inference_model(os.path.join(p, 'infer'), ['image'], eval_prog_target, exe, eval_program)

    # 保存成绩最好的模型
    if config.train['save_best_bleu_checkpoint']:
        if eval_bleu is not None and eval_bleu > logger.best_bleu:
            logger.best_bleu = eval_bleu
            fluid.io.save_persistables(exe, os.path.join(p, 'checkpoint_best_bleu'), train_program)
            if config.train['export_infer_model'] and eval_prog_target is not None:
                fluid.io.save_inference_model(os.path.join(p, 'infer_bleu'), ['image'], eval_prog_target, exe,
                                              eval_program)


def load_model(exe, places, prog):
    if logger.is_first_init:
        ImageCaptionModel.first_init(places)  # 特殊参数初始化
        p = config.dc['PretrainedMobileNetPath']
        if p is not None:
            fluid.io.load_vars(exe, p, prog, predicate=util.get_predicate(p, warning=False))
    else:  # 恢复上次训练的进度
        p = os.path.join(config.train['checkpoint_path'], 'checkpoint')
        fluid.io.load_persistables(exe, p, prog)
        if logger.train_encoder != config.model['encoder']['encoder_trainable']:
            logger.train_encoder = config.model['encoder']['encoder_trainable']
            if logger.train_encoder is True:
                p = config.dc['PretrainedMobileNetPath']
                fluid.io.load_vars(exe, p, prog, predicate=util.get_predicate(p, warning=False))


def train():
    loss, lrate, train_loader, train_startup, train_prog = training_net()
    caption, eval_startup, eval_prog = eval_net()

    places = fluid.CUDAPlace(0)
    exe = fluid.Executor(places)
    exe.run(train_startup)
    exe.run(eval_startup)

    strategy = fluid.ExecutionStrategy()
    strategy.num_iteration_per_drop_scope = 100
    train_exe = fluid.ParallelExecutor(use_cuda=True,
                                       loss_name='loss',
                                       main_program=train_prog,
                                       exec_strategy=strategy)
    eval_exe = fluid.ParallelExecutor(use_cuda=True,
                                      main_program=eval_prog,
                                      share_vars_from=train_exe)

    init_data_loader(train_loader, places, 'train')

    load_model(exe, places, train_prog)

    for epoch in range(logger.epoch, config.train['max_epoch'] + 1):
        logger.epoch = epoch
        begin_time = time.time()
        logger.log("Epoch {}".format(epoch))
        epoch_loss = 0
        for step, data in enumerate(train_loader()):
            step_loss, lr = train_exe.run(feed=data, fetch_list=[loss, lrate])
            if np.isnan(step_loss).any():  # 检查Nan
                raise AssertionError('Epoch:{} Step:{} Loss为Nan'.format(epoch, step + 1))
            epoch_loss += step_loss[0]

            # log
            if (step + 1) % config.train['log_every_n_step'] == 0:
                logger.log(' ' * 4 + 'Step {} Mean loss: {:6f} Step loss: {:6f}, lr: {}'.
                           format(step + 1, epoch_loss / (step + 1),
                                  step_loss[0], str(lr[0])))
        logger.log('Epoch loss: {:7f}'.format(epoch_loss / (step + 1)))

        # 计算验证集成绩
        dr = data_reader.get_reader(batch_size, 'dev')
        bleu_score = 0

        eval_begin_time = time.time()

        sentence_said = set()
        for l, data in enumerate(dr()):
            img, real_cap = zip(*data)
            # TODO
            # img = np.reshape(img, [-1, 1280, 49])
            batched_img = np.stack(img)
            cp = eval_exe.run(feed={'image': batched_img}, fetch_list=[caption])[0]
            bleu_score += evaluate.calc_bleu(cp, real_cap)
            for p in cp.tolist():
                sentence_said.add(evaluate.words2sentence(evaluate.filter(p)))
        bleu_score /= l + 1
        logger.log('Dev set: BLEU 分数: {:.7f} 语句数: {} 耗时: {:.2f}s'.
                   format(bleu_score, len(sentence_said), time.time() - eval_begin_time))

        # 保存模型
        save_model(exe, train_prog, eval_prog, epoch, [caption], bleu_score)
        logger.log('Epoch 耗时 {:2f}s'.format(time.time() - begin_time))


if __name__ == '__main__':
    try:
        train()
    except Exception as e:
        logger.log(str(e))
        raise e
