import torch
import torch.nn as nn
import pandas as pd
import numpy as np
from sklearn import metrics
import pickle
import os
from attentionwalk import AttentionWalkLayer
from evaluation import *
import pdb
import networkx as nx
from sklearn.model_selection import train_test_split


class Solver:
    def __init__(self, args):
        self.args = args
        self.model = None
        self.optimizer = None
        self.device = 'cpu'
        self.num_workers = 4

        self.is_directed = False
        self.num_nodes = 0
        self.test_neg_arr = None
        self.test_pos_arr = None
        self.train_pos_arr = None
        self.train_neg_arr = None
        self.transit_mat = None
        self.eval_metrics = None
        self.node_labels = None
        self.node_list_map = None

        self.prepare_graph()
        self.init_training()

    def prepare_graph(self):
        print('Loading graph....')

        dataset_dir = os.path.join('datasets', self.args.dataset)

        test_neg_file = os.path.join(dataset_dir, 'test.directed.neg.txt.npy')
        if os.path.exists(test_neg_file):
            self.is_directed = True
        else:
            self.is_directed = False
            test_neg_file = os.path.join(dataset_dir, 'test.neg.txt.npy')
        test_neg_arr = np.load(open(test_neg_file, 'rb'))

        test_pos_file = os.path.join(dataset_dir, 'test.txt.npy')
        test_pos_arr = np.load(open(test_pos_file, 'rb'))

        train_pos_file = os.path.join(dataset_dir, 'train.txt.npy')
        train_neg_file = os.path.join(dataset_dir, 'train.neg.txt.npy')
        train_pos_arr = np.load(open(train_pos_file, 'rb'))
        train_neg_arr = np.load(open(train_neg_file, 'rb'))

        index_file = os.path.join(dataset_dir, 'index.pkl')
        if os.path.exists(index_file):
            index = pickle.load(open(index_file, 'rb'))
            self.num_nodes = len(index['index'])
        else:
            G = nx.read_gpickle(os.path.join(dataset_dir, 'train.gpickle'))
            self.num_nodes = len(G.nodes())
            label_path = os.path.join(dataset_dir, 'node_labels.pickle')
            self.node_labels = pickle.load(open(label_path, 'rb')).toarray()
            label_map_path = os.path.join(dataset_dir, 'nodelistmap.pickle')
            self.node_list_map = pickle.load(open(label_map_path, 'rb'))

        self.test_neg_arr = test_neg_arr
        self.test_pos_arr = test_pos_arr
        self.train_pos_arr = train_pos_arr
        self.train_neg_arr = train_neg_arr

        adj_mat = np.zeros((self.num_nodes, self.num_nodes), dtype='float32')
        train_edges = np.load(open(os.path.join(dataset_dir, 'train.txt.npy'), 'rb'))
        adj_mat[train_edges[:, 0], train_edges[:, 1]] = 1.0
        if not self.is_directed:
            adj_mat[train_edges[:, 1], train_edges[:, 0]] = 1.0

        print('#Nodes', self.num_nodes)
        print('#Edges', len(train_edges))
        print('Is_directed', self.is_directed)
        print('Preparing graph...')

        # how to use GPUs
        os.environ['CUDA_VISIBLE_DEVICES'] = self.args.gpu
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.num_workers = max([4 * torch.cuda.device_count(), 4])

        transit_mat = adj_mat.T
        degree = transit_mat.sum(axis=0)
        transit_mat = transit_mat / (degree + 1e-7)
        self.transit_mat = torch.from_numpy(transit_mat)

        # # degrees = adj_mat.sum(axis=0)  # V
        # # diag = np.diag(degrees)
        # # diag = np.linalg.inv(diag)
        # # transit_mat = np.dot(diag, adj_mat) + 1e-7
        # transit_mat = torch.from_numpy(transit_mat).to(self.device)
        # transit_mat_series = [transit_mat]
        #
        # print('Preparing power series...')
        # if self.args.window_size > 1:
        #     for i in range(self.args.window_size-1):
        #         print('Computing T^{}....'.format(i + 2))
        #         transit_mat_series.append(torch.mm(transit_mat_series[-1], transit_mat))
        # self.transit_mat_series = torch.stack(transit_mat_series)  # CxVxV

    def init_training(self):
        print('Initializing training....')

        self.model = AttentionWalkLayer(self.num_nodes, self.args.emb_dim, self.args.window_size,
                                        self.args.n_walks, self.args.beta, self.args.gamma, self.args.attention, self.args.normalize, self.args.temperature, self.args.shared)

        if self.device == 'cuda':
            device_count = torch.cuda.device_count()
            if device_count > 1:
                self.model = nn.DataParallel(self.model)
            torch.backends.cudnn.benchmark = True
            print("Let's use {} GPUs!".format(device_count))
        self.model.to(self.device)

        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.args.lr)
        # self.optimizer = torch.optim.Adagrad(self.model.parameters(), lr=self.args.lr)
        # self.optimizer = torch.optim.SGD(self.model.parameters(), lr=self.args.lr, momentum)
        # self.scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, step_size=30)

        self.eval_metrics = {
            'epoch_at_best_train': 0,
            'best_train_auc': 0,
            'test_auc_at_best_train': 0,
            'attention': None,
            'left_emb': None,
            'right_emb': None
        }

    def train(self):
        print("Training the model....")
        self.model.train()
        train_auc = 0
        test_auc = 0
        # self.transit_mat_series = self.transit_mat_series.to(self.device)
        for epoch in range(self.args.epochs):
            self.optimizer.zero_grad()
            self.transit_mat = self.transit_mat.to(self.device)
            loss = self.model(self.transit_mat)
            loss.backward()
            self.optimizer.step()

            # self.scheduler.step(epoch)
            if epoch % 10 == 0 or epoch+1 == self.args.epochs:
                train_auc, test_auc, test_map = self.link_prediction_eval()
                nc_micro, nc_macro = self.node_classification_eval()

                if train_auc > self.eval_metrics['best_train_auc']:
                    self.eval_metrics['best_train_auc'] = train_auc
                    self.eval_metrics['test_auc_at_best_train'] = test_auc
                    self.eval_metrics['epoch_at_best_train'] = epoch
                    self.eval_metrics['attention'] = self.model.attention
                    self.eval_metrics['left_emb'] = self.model.left_emb
                    self.eval_metrics['right_emb'] = self.model.right_emb
                    self.eval_metrics['test_map_at_best_train'] = test_map
                    self.eval_metrics['test_micro_at_best_train'] = nc_micro
                    self.eval_metrics['test_macro_at_best_train'] = nc_macro

                print('Epoch: {:0>3d}/{}, '
                      'Loss: {:.2f}, '
                      'Train AUC: {:.4f}, '
                      'Test AUC: {:.4f}, '
                      'Best Train AUC: {:.4f}, '
                      'Test AUC at Best Train: {:.4f}, '
                      'Test MAP at Best Train: {:.4f}, '
                      'Test Micro NC at Best Train: {:.4f}, '
                      'Test Macro NC at Best Train: {:.4f}, '
                      'Epoch at Best Train: {:0>3d}'.format(epoch+1, self.args.epochs,
                                                            loss,
                                                            train_auc,
                                                            test_auc,
                                                            self.eval_metrics['best_train_auc'],
                                                            self.eval_metrics['test_auc_at_best_train'],
                                                            self.eval_metrics['test_map_at_best_train'],
                                                            self.eval_metrics['test_micro_at_best_train'],
                                                            self.eval_metrics['test_macro_at_best_train'],
                                                            self.eval_metrics['epoch_at_best_train']+1
                                                            ))

                if epoch - self.eval_metrics['epoch_at_best_train'] >= 50:
                    print('The model seems to be overfitting...')
                    break

    def node_classification_eval(self, test_ratio=0.3):
        micro, macro = 0, 0

        if self.node_labels is None:
            print("Node labels are not provided...")
            return micro, macro

        if self.args.shared:
            embeds = self.model.left_emb.detach().to('cpu').numpy()
        else:
            embeds = torch.cat((self.model.left_emb, self.model.right_emb), dim=1).detach().to('cpu').numpy()

        temp_map = {v:k for k, v in self.node_list_map.items()}
        embeds = torch.cat((self.model.left_emb, self.model.right_emb), dim=1).detach().to('cpu').numpy()
        temp_map = {v: k for k, v in self.node_list_map.items()}

        labels = np.array([self.node_labels[temp_map[i]] for i in range(self.num_nodes)])
        X_tr, X_te, y_tr, y_te = train_test_split(embeds,
                                                  labels,
                                                  test_size=test_ratio,
                                                  random_state=0)

        micro, macro = eval_node_classification(X_tr, y_tr, X_te, y_te)

        return micro, macro

    def link_prediction_eval(self):
        """Calls sess.run(g) and computes AUC metric for test and train."""

        scores = torch.mm(self.model.left_emb, self.model.right_emb.transpose(0, 1)).detach().to('cpu').numpy()

        # Compute train auc:
        train_pos_prods = scores[self.train_pos_arr[:, 0], self.train_pos_arr[:, 1]]
        train_neg_prods = scores[self.train_neg_arr[:, 0], self.train_neg_arr[:, 1]]
        train_y = [0] * len(train_neg_prods) + [1] * len(train_pos_prods)
        train_y_pred = np.concatenate([train_neg_prods, train_pos_prods], 0)
        train_auc = metrics.roc_auc_score(train_y, train_y_pred)

        # Compute test auc:
        if len(self.test_pos_arr) > 0:
            test_pos_prods = scores[self.test_pos_arr[:, 0], self.test_pos_arr[:, 1]]
            test_neg_prods = scores[self.test_neg_arr[:, 0], self.test_neg_arr[:, 1]]
            test_y = [0] * len(test_neg_prods) + [1] * len(test_pos_prods)
            test_y_pred = np.concatenate([test_neg_prods, test_pos_prods], 0)
            test_auc = metrics.roc_auc_score(test_y, test_y_pred)

            test_map = eval_link_prediction(self.model.left_emb,
                                            self.model.right_emb,
                                            self.test_pos_arr,
                                            self.train_pos_arr,
                                            is_directed=self.is_directed)
        else:
            test_auc = 0
            test_map = 0

        return train_auc, test_auc, test_map

    def save_embedding(self):
        print("Saving the embedding....")
        left_emb = self.eval_metrics['left_emb'].detach().to('cpu').numpy()
        right_emb = self.eval_metrics['right_emb'].to('cpu').detach().numpy()
        indices = np.arange(self.num_nodes).reshape(-1, 1)
        embedding = np.concatenate([indices, left_emb, right_emb], axis=1)
        columns = ["id"] + ["x_" + str(x) for x in range(self.args.emb_dim)]
        embedding = pd.DataFrame(embedding, columns=columns)
        embedding_path = os.path.join(self.args.output, '{}_{}_embedding.csv'.format(self.args.dataset, self.args.attention))
        embedding.to_csv(embedding_path, index=None)

    def save_attention(self):
        print("Saving the attention....")
        if self.args.attention in ('global_exponential', 'personalized_exponential'):
            q = self.model.q.detach().to('cpu').numpy()
            q = pd.DataFrame(q)

            q_path = os.path.join(self.args.output, '{}_{}_q.csv'.format(self.args.dataset, self.args.attention))
            q.to_csv(q_path, index=None)
        elif self.args.attention in ['global_gamma', 'personalized_gamma']:
            k = self.model.k.detach().to('cpu').numpy().reshape(-1, 1)
            theta = self.model.theta.detach().to('cpu').numpy().reshape(-1, 1)
            data = np.concatenate((k, theta), axis=1)
            df = pd.DataFrame(data, columns=['k', 'theta'])
            path = os.path.join(self.args.output, '{}_{}_k_theta.csv'.format(self.args.dataset, self.args.attention))
            df.to_csv(path, index=None)
        elif self.args.attention in ['global_quadratic', 'personalized_quadratic']:
            a = self.model.a.detach().to('cpu').numpy().reshape(-1, 1)
            b = self.model.b.detach().to('cpu').numpy().reshape(-1, 1)
            c = self.model.c.detach().to('cpu').numpy().reshape(-1, 1)
            data = np.concatenate((a, b, c), axis=1)
            df = pd.DataFrame(data, columns=['a', 'b', 'c'])
            path = os.path.join(self.args.output, '{}_{}_a_b_c.csv'.format(self.args.dataset, self.args.attention))
            df.to_csv(path, index=None)
        attention = nn.functional.softmax(self.eval_metrics['attention'], dim=0).detach().to('cpu').numpy().reshape(self.args.window_size, -1)
        indices = np.arange(self.args.window_size).reshape(-1, 1)
        attention = np.concatenate([indices, attention], axis=1)
        attention = pd.DataFrame(attention).rename(columns={0: 'Order'})
        attention_path = os.path.join(self.args.output, '{}_{}_attention.csv'.format(self.args.dataset, self.args.attention))
        attention.to_csv(attention_path, index=None)

    def save_results(self):
        print("Saving the results....")
        results = 'Best Train AUC: {:.4f}, ' \
                  'Test AUC at Best Train: {:.4f}, ' \
                  'Test MAP at Best Train: {:.4f}, ' \
                  'Test Micro NC at Best Train: {:.4f}, ' \
                  'Test Macro NC at Best Train: {:.4f}, ' \
                  'Epoch at Best Train: {:0>3d}'.format(self.eval_metrics['best_train_auc'],
                    self.eval_metrics['test_auc_at_best_train'],
                    self.eval_metrics['test_map_at_best_train'],
                    self.eval_metrics['test_micro_at_best_train'],
                    self.eval_metrics['test_macro_at_best_train'],
                    self.eval_metrics['epoch_at_best_train'])
        path = os.path.join(self.args.output, '{}_{}_results.txt'.format(self.args.dataset, self.args.attention))
        with open(path, mode='w') as f:
            f.write(results)

    def save(self):
        if not os.path.exists(self.args.output):
            os.mkdir(self.args.output)
        self.save_embedding()
        self.save_attention()
        self.save_results()
