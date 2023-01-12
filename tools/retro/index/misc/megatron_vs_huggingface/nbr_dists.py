# Copyright (c) 2023, NVIDIA CORPORATION.  All rights reserved.

from collections import defaultdict
import faiss
import numpy as np
import torch

from megatron import get_retro_args
from tools.bert_embedding import BertEmbedder
from tools.retro.db.utils import (
    get_merged_train_dataset,
    get_merged_valid_dataset,
)
from tools.retro.utils import GPTToTextDataset


def get_datasets():
    gpt_datasets = {
        "train" : get_merged_train_dataset(),
        "valid" : get_merged_valid_dataset(),
    }
    text_datasets = { k: GPTToTextDataset(d) for k, d in gpt_datasets.items() }
    return text_datasets


def get_embedders():
    args = get_retro_args()
    return {
        "megatron" : BertEmbedder(
            args.retro_bert_batch_size,
            args.retro_bert_max_chunk_length,
            embedder_type = "megatron",
        ),
        "huggingface" : BertEmbedder(
            args.retro_bert_batch_size,
            args.retro_bert_max_chunk_length,
            embedder_type = "huggingface",
        ),
    }


def get_indexes():

    args = get_retro_args()

    # Read indexes.
    indexes = {
        "megatron" : faiss.read_index("/path/to/index", faiss.IO_FLAG_MMAP),
        "huggingface" : faiss.read_index("/path/to/index", faiss.IO_FLAG_MMAP),
    }

    assert len(set([ index.ntotal for index in indexes.values() ])) == 1

    # Search parameters.
    for index in indexes.values():
        faiss.ParameterSpace().set_index_parameter(index, "efSearch",
                                                   args.retro_ef_search)
        faiss.ParameterSpace().set_index_parameter(index, "nprobe",
                                                   args.retro_nprobe)

    return indexes


class TextListDataset(torch.utils.data.Dataset):
    '''Dataset that holds single string.'''
    def __init__(self, texts):
        assert isinstance(texts, list)
        for text in texts:
            assert isinstance(text, str)
        self.texts = texts
    def __len__(self):
        return len(self.texts)
    def __getitem__(self, i):
        return {"text": self.texts[i]}


def compare_bert_neighbor_dists():

    if torch.distributed.get_rank() != 0:
        return

    datasets = get_datasets()
    embedders = get_embedders()
    indexes = get_indexes()
    # max_neighbors = 5
    # max_neighbors = 40
    max_neighbors = 200

    valid_text_subset = torch.utils.data.Subset(
        datasets["valid"],
        range(0, len(datasets["valid"]), len(datasets["valid"]) // 10),
    )
    query_embeddings = {
        k : e.embed_text_dataset(valid_text_subset)
        for k, e in embedders.items()
    }
    neighbors = {
        k : i.search(query_embeddings[k], max_neighbors)[1]
        for k, i in indexes.items()
    }

    from tools.retro.cli import shorten_str
    self_neighbor_dists = defaultdict(list)
    cross_neighbor_dists = defaultdict(list)
    for valid_idx in range(len(valid_text_subset)):

        print("valid_idx %d / %d." % (valid_idx, len(valid_text_subset)))

        megatron_neighbor_ids = neighbors["megatron"][valid_idx]
        huggingface_neighbor_ids = neighbors["huggingface"][valid_idx]
        neighbor_texts = {
            "megatron" : TextListDataset([datasets["train"][i]["text"]
                                          for i in megatron_neighbor_ids]),
            "huggingface" : TextListDataset([datasets["train"][i]["text"]
                                             for i in huggingface_neighbor_ids]),
        }

        self_neighbor_embeddings = {
            "megatron" :
            embedders["megatron"].embed_text_dataset(neighbor_texts["megatron"]),
            "huggingface" :
            embedders["huggingface"].embed_text_dataset(neighbor_texts["huggingface"]),
        }
        cross_neighbor_embeddings = {
            "megatron" :
            embedders["megatron"].embed_text_dataset(neighbor_texts["huggingface"]),
            "huggingface" :
            embedders["huggingface"].embed_text_dataset(neighbor_texts["megatron"]),
        }

        for k in self_neighbor_embeddings:
            # self_neighbor_dists[k].append(np.mean([
            self_neighbor_dists[k].append([
                np.linalg.norm(query_embeddings[k][valid_idx] - e)
                for e in self_neighbor_embeddings[k]])
        for k in cross_neighbor_embeddings:
            # cross_neighbor_dists[k].append(np.mean([
            cross_neighbor_dists[k].append([
                np.linalg.norm(query_embeddings[k][valid_idx] - e)
                for e in cross_neighbor_embeddings[k]])

    print("~~ self neighbor dists ~~")
    print({k:np.mean(d) for k,d in self_neighbor_dists.items()})
    print("~~ cross neighbor dists ~~")
    print({k:np.mean(d) for k,d in cross_neighbor_dists.items()})
    
    print("~~ top-n diffs ~~")
    for k, dist_lists in self_neighbor_dists.items():
        [ dists.sort() for dists in dist_lists ]
    self_neighbor_dists = {k:np.mean(dd, axis = 0) for k, dd in self_neighbor_dists.items()}
    top_diffs = {k:{
        "top1" : (dd[1] - dd[0]) / dd[0],
        "top2" : (dd[2] - dd[1]) / dd[1],
        "top5" : (dd[5] - dd[4]) / dd[4],
        "top20" : (dd[20] - dd[19]) / dd[19],
        "topn" : (dd[-1] - dd[-2]) / dd[-2],
    } for k, dd in self_neighbor_dists.items()}
    print(self_neighbor_dists)
    print(top_diffs)
    exit()
