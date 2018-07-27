import sys
import os
import torch
import random
import numpy as np
from tqdm import tqdm
from torch.autograd import Variable
from torch.nn.parameter import Parameter
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import math
import pdb
from DGCNN_embedding import DGCNN
from mlp_dropout import MLPClassifier

sys.path.append('%s/pytorch_structure2vec-master/s2v_lib' % os.path.dirname(os.path.realpath(__file__)))
from embedding import EmbedMeanField, EmbedLoopyBP

from util import cmd_args, load_data

model_folder = "model_folder/"

class Classifier(nn.Module):
    def __init__(self):
        super(Classifier, self).__init__()
        if cmd_args.gm == 'mean_field':
            model = EmbedMeanField
        elif cmd_args.gm == 'loopy_bp':
            model = EmbedLoopyBP
        elif cmd_args.gm == 'DGCNN':
            model = DGCNN
        else:
            print('unknown gm %s' % cmd_args.gm)
            sys.exit()

        if cmd_args.gm == 'DGCNN':
            self.s2v = model(latent_dim=cmd_args.latent_dim,
                            output_dim=cmd_args.out_dim,
                            num_node_feats=cmd_args.feat_dim+cmd_args.attr_dim,
                            num_edge_feats=0,
                            k=cmd_args.sortpooling_k)
        else:
            self.s2v = model(latent_dim=cmd_args.latent_dim,
                            output_dim=cmd_args.out_dim,
                            num_node_feats=cmd_args.feat_dim,
                            num_edge_feats=0,
                            max_lv=cmd_args.max_lv)
        out_dim = cmd_args.out_dim
        if out_dim == 0:
            if cmd_args.gm == 'DGCNN':
                out_dim = self.s2v.dense_dim
            else:
                out_dim = cmd_args.latent_dim
        self.mlp = MLPClassifier(input_size=out_dim, hidden_size=cmd_args.hidden, num_class=cmd_args.num_class, with_dropout=cmd_args.dropout)

    def PrepareFeatureLabel(self, batch_graph):
        labels = torch.LongTensor(len(batch_graph))
        n_nodes = 0

        if batch_graph[0].node_tags is not None:
            node_tag_flag = True
            concat_tag = []
        else:
            node_tag_flag = False

        if batch_graph[0].node_features is not None:
            node_feat_flag = True
            concat_feat = []
        else:
            node_feat_flag = False

        for i in range(len(batch_graph)):
            labels[i] = batch_graph[i].label
            n_nodes += batch_graph[i].num_nodes
            if node_tag_flag == True:
                concat_tag += batch_graph[i].node_tags
            if node_feat_flag == True:
                tmp = torch.from_numpy(batch_graph[i].node_features).type('torch.FloatTensor')
                concat_feat.append(tmp)

        if node_tag_flag == True:
            concat_tag = torch.LongTensor(concat_tag).view(-1, 1)
            node_tag = torch.zeros(n_nodes, cmd_args.feat_dim)
            node_tag.scatter_(1, concat_tag, 1)

        if node_feat_flag == True:
            node_feat = torch.cat(concat_feat, 0)

        if node_feat_flag and node_tag_flag:
            # concatenate one-hot embedding of node tags (node labels) with continuous node features
            node_feat = torch.cat([node_tag.type_as(node_feat), node_feat], 1)
        elif node_feat_flag == False and node_tag_flag == True:
            node_feat = node_tag
        elif node_feat_flag == True and node_tag_flag == False:
            pass
        else:
            node_feat = torch.ones(n_nodes, 1)  # use all-one vector as node features

        if cmd_args.mode == 'gpu':
            node_feat = node_feat.cuda()
            labels = labels.cuda()

        return node_feat, labels

    def forward(self, batch_graph):
        node_feat, labels = self.PrepareFeatureLabel(batch_graph)
        embed = self.s2v(batch_graph, node_feat, None)

        return self.mlp(embed, labels)

def loop_dataset(g_list, classifier, sample_idxes, optimizer=None, bsize=cmd_args.batch_size):
    total_loss = []
    total_iters = (len(sample_idxes) + (bsize - 1) * (optimizer is None)) // bsize
    pbar = tqdm(range(total_iters), unit='batch')

    n_samples = 0
    for pos in pbar:
        selected_idx = sample_idxes[pos * bsize : (pos + 1) * bsize]

        batch_graph = [g_list[idx] for idx in selected_idx]
        _, loss, acc = classifier(batch_graph)

        if optimizer is not None:
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        loss = loss.data.cpu().numpy()
        pbar.set_description('loss: %0.5f acc: %0.5f' % (loss, acc) )
        #pbar.set_description((loss, acc))

        total_loss.append( np.array([loss, acc]) * len(selected_idx))

        n_samples += len(selected_idx)
    if optimizer is None:
        assert n_samples == len(sample_idxes)
    total_loss = np.array(total_loss)
    avg_loss = np.sum(total_loss, 0) / n_samples
    return avg_loss, acc


if __name__ == '__main__':
    random.seed(10*cmd_args.seed)
    np.random.seed(10*cmd_args.seed)
    torch.manual_seed(10*cmd_args.seed)
    print('Patience value: ', int(cmd_args.patience))

    list_learning_rate = [cmd_args.learning_rate]
    train_graphs, validation_graphs, test_graphs = load_data()
    print('# train: %d, # test: %d' % (len(train_graphs), len(test_graphs)))

    if cmd_args.sortpooling_k <= 1:
        num_nodes_list = sorted([g.num_nodes for g in train_graphs + test_graphs])
        cmd_args.sortpooling_k = num_nodes_list[int(math.ceil(cmd_args.sortpooling_k * len(num_nodes_list))) - 1]
        print('k used in SortPooling is: ' + str(cmd_args.sortpooling_k))

    list_best_val_loss = []
    list_best_val_acc = []
    list_nb_epoch = []
    for lr_idx, lr in enumerate(list_learning_rate):
        best_model_path = model_folder + cmd_args.data + "_" + str(cmd_args.seed) + "_" + str(cmd_args.fold) + "_" + str(lr_idx)
        classifier = Classifier()
        if cmd_args.mode == 'gpu':
            classifier = classifier.cuda()

        optimizer = optim.Adam(classifier.parameters(), lr=lr)

        train_idxes = list(range(len(train_graphs)))
    
        best_loss = 10
        best_acc = 0	
        patience_count = 0
        count_epoch = 0

        for epoch in range(cmd_args.num_epochs):
            random.shuffle(train_idxes)
            classifier.train()
            avg_loss, _ = loop_dataset(train_graphs, classifier, train_idxes, optimizer=optimizer)
            print('\033[92maverage training of epoch %d: loss %.5f acc %.5f\033[0m' % (epoch, avg_loss[0], avg_loss[1]))

            classifier.eval()
            validation_loss, validation_acc = loop_dataset(validation_graphs, classifier, list(range(len(validation_graphs))))
            print('\033[93maverage validation of epoch %d: loss %.5f acc %.5f\033[0m' % (epoch, validation_loss[0], validation_loss[1]))
	
	    if validation_loss[0] < best_loss:
	        torch.save(classifier.state_dict(), best_model_path)
                best_loss = validation_loss[0]
                patience_count = 0

            #if validation_loss[1] > best_acc:
            #    torch.save(classifier.state_dict(), best_model_path)
            #    best_acc = validation_loss[1]
            #    patience_count = 0
            else:
	        patience_count+=1
            count_epoch+=1
	    if patience_count >= cmd_args.patience:
                break
        list_nb_epoch.append(count_epoch)
        list_best_val_loss.append(best_loss)
        #list_best_val_acc.append(best_acc)
    optimal_model_idx = list_best_val_loss.index(min(list_best_val_loss))
    #optimal_model_idx = list_best_val_acc.index(max(list_best_val_acc))

    optimal_model_path = model_folder + cmd_args.data + "_" + str(cmd_args.seed) + "_" + str(cmd_args.fold) + "_" + str(optimal_model_idx)
    optimal_model = Classifier()
    if cmd_args.mode == 'gpu':
        optimal_model = optimal_model.cuda()
    optimal_model.load_state_dict(torch.load(optimal_model_path))
    optimal_model.eval()
    test_loss, test_acc = loop_dataset(test_graphs, optimal_model, list(range(len(test_graphs))))
    with open(str(cmd_args.result_file), 'a+') as f:
        f.write(str(test_loss[1]) + '\n')

    print('Going to save parameters values')
    with open(str(cmd_args.result_paras), 'a+') as f1:
        f1.write('Random ' + str(cmd_args.seed) + ', Fold ' + str(cmd_args.fold) + '\n')
        f1.write('OPTIMAL LEARNING RATE: ' + str(optimal_model_idx) + '\n')
        f1.write('OPTIMAL LOSS: ' + str(min(list_best_val_loss)) + '\n')
        #f1.write('OPTIMAL ACC: ' + str(max(list_best_val_acc)) + '\n')
        f1.write('Number of epoches: ' + str(list_nb_epoch[0]) +'\n')
        f1.write('==========================\n')

