import transformers
import torch
import os
import json
import random
import numpy as np
import argparse
from torch.utils.tensorboard import SummaryWriter
from datetime import datetime
from tqdm import tqdm
from torch.nn import DataParallel
import logging
from transformers.modeling_gpt2 import GPT2Config, GPT2LMHeadModel
from transformers import BertTokenizer
from os.path import join, exists
from itertools import zip_longest, chain
from torch.utils.data import Dataset, DataLoader
from torch.nn import CrossEntropyLoss
from sklearn.model_selection import train_test_split
import torch.nn.functional as F
import copy
import opencc2
from torch.utils.data import Dataset


# dataset.py
class MyDataset(Dataset):
    def __init__(self, data_list):
        self.data_list = data_list

    def __getitem__(self, index):
        input_ids = self.data_list[index].strip()
        input_ids = [int(token_id) for token_id in input_ids.split()]
        return input_ids

    def __len__(self):
        return len(self.data_list)



# train.py
def create_model(args, vocab_size):
    """

    :param args:
    :param vocab_size:字典大小
    :return:
    """
    if args.pretrained_model:  # 如果指定了预训练的GPT2模型
        model = GPT2LMHeadModel.from_pretrained(args.pretrained_model)
    else:  # 若没有指定预训练模型，则初始化模型
        model_config = transformers.modeling_gpt2.GPT2Config.from_json_file(args.model_config)
        model = GPT2LMHeadModel(config=model_config)
    # 根据tokenizer的vocabulary调整GPT2模型的voca的大小
    model.resize_token_embeddings(vocab_size)
    logger.info('model config:\n{}'.format(model.config.to_json_string()))
    return model, model.config.to_dict().get("n_ctx")



PAD = '[PAD]'
pad_id = 0

opencc_trad = opencc2.Converter(from_variant='cn', to_variant='hk', with_phrases=False, fast=True)
opencc_simp = opencc2.Converter(from_variant='hk', to_variant='cn', with_phrases=False, fast=True)

def set_interact_args():
    """
    Sets up the training arguments.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', default='0', type=str, required=False, help='生成设备')
    parser.add_argument('--temperature', default=1, type=float, required=False, help='生成的temperature')
    parser.add_argument('--topk', default=8, type=int, required=False, help='最高k选1')
    parser.add_argument('--topp', default=0, type=float, required=False, help='最高积累概率')
    parser.add_argument('--model_config', default='config/model_config_dialogue_small.json', type=str, required=False,
                        help='模型参数')
    parser.add_argument('--voca_path', default='vocab_small.txt', type=str, required=False, help='选择词库')
    parser.add_argument('--dialogue_model_path', default='dialogue_model/', type=str, required=False,
                        help='dialogue_model路径')
    parser.add_argument('--mmi_model_path', default='mmi_model/', type=str, required=False,
                        help='互信息mmi_model路径')
    parser.add_argument('--repetition_penalty', default=1.5, type=float, required=False,
                        help="重复惩罚参数，若生成的对话重复性较高，可适当提高该参数")
    parser.add_argument('--seed', type=int, default=None, help='设置种子用于生成随机数，以使得训练的结果是确定的')
    parser.add_argument('--max_len', type=int, default=25, help='每个utterance的最大长度,超过指定长度则进行截断')
    parser.add_argument('--max_history_len', type=int, default=5, help="dialogue history的最大长度")
    parser.add_argument('--no_cuda', action='store_true', help='不使用GPU进行预测')
    parser.add_argument('--batch_size', type=int, default=5, help='批量生成response，然后经过MMI模型进行筛选')
    parser.add_argument('--debug', action='store_true', help='指定该参数，可以查看生成的所有候选的reponse，及其loss')
    return parser.parse_args()


def create_logger(args):
    """
    将日志输出到日志文件和控制台
    """
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.ERROR)

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    return logger


def top_k_top_p_filtering(logits, top_k=0, top_p=0.0, filter_value=-float('Inf')):
    """ Filter a distribution of logits using top-k and/or nucleus (top-p) filtering
        Args:
            logits: logits distribution shape (vocabulary size)
            top_k > 0: keep only top k tokens with highest probability (top-k filtering).
            top_p > 0.0: keep the top tokens with cumulative probability >= top_p (nucleus filtering).
                Nucleus filtering is described in Holtzman et al. (http://arxiv.org/abs/1904.09751)
    """
    assert logits.dim() == 2
    top_k = min(top_k, logits[0].size(-1))  # Safety check
    if top_k > 0:
        # Remove all tokens with a probability less than the last token of the top-k
        # torch.topk()返回最后一维最大的top_k个元素，返回值为二维(values,indices)
        # ...表示其他维度由计算机自行推断
        for logit in logits:
            indices_to_remove = logit < torch.topk(logit, top_k)[0][..., -1, None]
            logit[indices_to_remove] = filter_value  # 对于topk之外的其他元素的logits值设为负无穷

    if top_p > 0.0:
        sorted_logits, sorted_indices = torch.sort(logits, descending=True, dim=-1)  # 对logits进行递减排序
        cumulative_probs = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)

        # Remove tokens with cumulative probability above the threshold
        sorted_indices_to_remove = cumulative_probs > top_p
        # Shift the indices to the right to keep also the first token above the threshold
        sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
        sorted_indices_to_remove[..., 0] = 0
        for index, logit in enumerate(logits):
            indices_to_remove = sorted_indices[index][sorted_indices_to_remove[index]]
            logit[indices_to_remove] = filter_value
    return logits


def main():
    args = set_interact_args()
    logger = create_logger(args)
    # 当用户使用GPU,并且GPU可用时
    args.cuda = torch.cuda.is_available() and not args.no_cuda
    device = 'cuda' if args.cuda else 'cpu'
    logger.info('using device:{}'.format(device))
    os.environ["CUDA_VISIBLE_DEVICES"] = args.device
    tokenizer = BertTokenizer(vocab_file=args.voca_path)
    # 对话model
    dialogue_model = GPT2LMHeadModel.from_pretrained(args.dialogue_model_path)
    dialogue_model.to(device)
    dialogue_model.eval()
    # 互信息mmi model
    mmi_model = GPT2LMHeadModel.from_pretrained(args.mmi_model_path)
    mmi_model.to(device)
    mmi_model.eval()
    history = []
    print('开始和chatbot聊天，输入CTRL + Z以退出')

    while True:
        try:
            text = input("user:")
            text = opencc_simp.convert(text)
            history.append(tokenizer.encode(text))
            input_ids = [tokenizer.cls_token_id]  # 每个input以[CLS]为开头
            for history_id, history_utr in enumerate(history[-args.max_history_len:]):
                input_ids.extend(history_utr)
                input_ids.append(tokenizer.sep_token_id)
            # 用于批量生成response，维度为(batch_size,token_len)
            input_ids = [copy.deepcopy(input_ids) for _ in range(args.batch_size)]

            curr_input_tensors = torch.tensor(input_ids).long().to(device)
            generated = []  # 二维数组，维度为(生成的response的最大长度，batch_size)，generated[i,j]表示第j个response的第i个token的id
            finish_set = set()  # 标记是否所有response均已生成结束，若第i个response生成结束，即生成了sep_token_id，则将i放入finish_set
            # 最多生成max_len个token
            for _ in range(args.max_len):
                outputs = dialogue_model(input_ids=curr_input_tensors)
                next_token_logits = outputs[0][:, -1, :]
                # 对于已生成的结果generated中的每个token添加一个重复惩罚项，降低其生成概率
                for index in range(args.batch_size):
                    for token_id in set([token_ids[index] for token_ids in generated]):
                        next_token_logits[index][token_id] /= args.repetition_penalty
                next_token_logits = next_token_logits / args.temperature
                # 对于[UNK]的概率设为无穷小，也就是说模型的预测结果不可能是[UNK]这个token
                for next_token_logit in next_token_logits:
                    next_token_logit[tokenizer.convert_tokens_to_ids('[UNK]')] = -float('Inf')
                    # 同理，屏蔽與男性相關的詞彙
                    next_token_logit[tokenizer.convert_tokens_to_ids('男')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('帥')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('公')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('哥')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('兄')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('弟')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('爸')] = -float('Inf')
                    # 同理，屏蔽詈詞
                    next_token_logit[tokenizer.convert_tokens_to_ids('妈')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('臭')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('草')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('肏')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('嗨')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('死')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('屎')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('骂')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('逼')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('残')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('揍')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('傻')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('害')] = -float('Inf')
                    next_token_logit[tokenizer.convert_tokens_to_ids('呸')] = -float('Inf')
                filtered_logits = top_k_top_p_filtering(next_token_logits, top_k=args.topk, top_p=args.topp)
                # torch.multinomial表示从候选集合中无放回地进行抽取num_samples个元素，权重越高，抽到的几率越高，返回元素的下标
                next_token = torch.multinomial(F.softmax(filtered_logits, dim=-1), num_samples=1)
                # 判断是否有response生成了[SEP],将已生成了[SEP]的resposne进行标记
                for index, token_id in enumerate(next_token[:, 0]):
                    if token_id == tokenizer.sep_token_id:
                        finish_set.add(index)
                # 检验是否所有的response均已生成[SEP]
                finish_flag = True  # 是否所有的response均已生成[SEP]的token
                for index in range(args.batch_size):
                    if index not in finish_set:  # response批量生成未完成
                        finish_flag = False
                        break
                if finish_flag:
                    break
                generated.append([token.item() for token in next_token[:, 0]])
                # 将新生成的token与原来的token进行拼接
                curr_input_tensors = torch.cat((curr_input_tensors, next_token), dim=-1)
            candidate_responses = []  # 生成的所有候选response
            for batch_index in range(args.batch_size):
                response = []
                for token_index in range(len(generated)):
                    if generated[token_index][batch_index] != tokenizer.sep_token_id:
                        response.append(generated[token_index][batch_index])
                    else:
                        break
                candidate_responses.append(response)

            # mmi模型的输入=
            min_loss = float('Inf')
            best_response = ""
            for response in candidate_responses:
                mmi_input_id = [tokenizer.cls_token_id]  # 每个input以[CLS]为开头
                mmi_input_id.extend(response)
                mmi_input_id.append(tokenizer.sep_token_id)
                for history_utr in reversed(history[-args.max_history_len:]):
                    mmi_input_id.extend(history_utr)
                    mmi_input_id.append(tokenizer.sep_token_id)
                mmi_input_tensor = torch.tensor(mmi_input_id).long().to(device)
                out = mmi_model(input_ids=mmi_input_tensor, labels=mmi_input_tensor)
                loss = out[0].item()
                if loss < min_loss:
                    best_response = response
                    min_loss = loss
            history.append(best_response)
            text = tokenizer.convert_ids_to_tokens(best_response)
            text = "".join(text)
            if text == '图片评论':
                text = '😭️😭️😭️😭️😭️😭️'
            else:
                text = opencc_trad.convert(text)
            print("chatbot:" + text)
        except KeyboardInterrupt:
            break


if __name__ == '__main__':
    main()