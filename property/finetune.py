import argparse
from loader import MoleculeDataset,DataLoaderMasking
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from tqdm import tqdm
import numpy as np
from model import MolGT_graphpred
from sklearn.metrics import roc_auc_score
from splitters import scaffold_split,random_scaffold_split,random_split,scaffold_split_fp
import pandas as pd
import os
from util import *
import warnings,random
warnings.filterwarnings("ignore")

def disable_rdkit_logging():
    """
    Disables RDKit whiny logging.
    """
    import rdkit.rdBase as rkrb
    import rdkit.RDLogger as rkl
    logger = rkl.logger()
    logger.setLevel(rkl.ERROR)
    rkrb.DisableLog('rdApp.error')

disable_rdkit_logging()
#Workaround because python functions are not picklable
class WorkerInitObj(object):
    def __init__(self, seed):
        self.seed = seed
    def __call__(self, id):
        np.random.seed(seed=self.seed + id)
        random.seed(self.seed + id)

def train(args, model, device, loader, optimizer,criterion):
    model.train()

    for step, batch in enumerate(tqdm(loader, desc="Iteration")):
        batch = batch.to(device)
        pred = model(batch)
        y = batch.y.view(pred.shape).to(torch.float64)

        #Whether y is non-null or not.
        is_valid = y**2 > 0
        #Loss matrix
        loss_mat = criterion(pred.double(), (y+1)/2)
        #loss matrix after removing null target
        loss_mat = torch.where(is_valid, loss_mat, torch.zeros(loss_mat.shape).to(loss_mat.device).to(loss_mat.dtype))
            
        optimizer.zero_grad()
        loss = torch.sum(loss_mat)/torch.sum(is_valid)
        loss.backward()

        optimizer.step()

def eval(args, model, device, loader):
    model.eval()
    y_true = []
    y_scores = []

    for step, batch in enumerate(tqdm(loader, desc="Iteration")):
        batch = batch.to(device)
        with torch.no_grad():
            pred = model(batch)
        y_true.append(batch.y.view(pred.shape))
        y_scores.append(pred)

    y_true = torch.cat(y_true, dim = 0).cpu().numpy()
    y_scores = torch.cat(y_scores, dim = 0).cpu().numpy()

    roc_list = []
    for i in range(y_true.shape[1]):
        #AUC is only defined when there is at least one positive data.
        if np.sum(y_true[:,i] == 1) > 0 and np.sum(y_true[:,i] == -1) > 0:
            is_valid = y_true[:,i]**2 > 0
            roc_list.append(roc_auc_score((y_true[is_valid,i] + 1)/2, y_scores[is_valid,i]))

    if len(roc_list) < y_true.shape[1]:
        print("Some target is missing!")
        print("Missing ratio: %f" %(1 - float(len(roc_list))/y_true.shape[1]))

    return sum(roc_list)/len(roc_list) #y_true.shape[1]

def main():
    # Training settings
    parser = argparse.ArgumentParser(description='PyTorch implementation of pre-training of graph neural networks')
    parser.add_argument('--device', type=int, default=0,
                        help='which gpu to use if any (default: 0)')
    parser.add_argument('--batch_size', type=int, default=32,
                        help='input batch size for training (default: 32)')
    parser.add_argument('--epochs', type=int, default=100,
                        help='number of epochs to train (default: 100)')
    parser.add_argument('--lr', type=float, default=0.0001,
                        help='learning rate (default: 0.001)')
    parser.add_argument('--lr_decay', type=float, default=0.995,
                        help='learning rate decay (default: 0.995)')
    parser.add_argument('--lr_scale', type=float, default=1,
                        help='relative learning rate for the feature extraction layer (default: 1)')
    parser.add_argument('--decay', type=float, default=0,
                        help='weight decay (default: 0)')
    parser.add_argument('--loss_type', type=str, default="bce")
    parser.add_argument('--num_layer', type=int, default=5,
                        help='number of GNN message passing layers (default: 5).')
    parser.add_argument('--emb_dim', type=int, default=768,
                        help='embedding dimensions (default: 300)')
    parser.add_argument('--heads', type=int, default=12,
                        help='multi heads (default: 4)')
    parser.add_argument('--num_message_passing', type=int, default=3,
                        help='message passing steps (default: 3)')
    parser.add_argument('--dropout_ratio', type=float, default=0.5,
                        help='dropout ratio (default: 0.5)')
    parser.add_argument('--graph_pooling', type=str, default="set2set",
                        help='graph level pooling (collection,sum, mean, max, set2set, attention)')
    parser.add_argument('--JK', type=str, default="last",
                        help='how the node features across layers are combined. last, sum, max or concat')
    parser.add_argument('--gnn_type', type=str, default="gin")
    parser.add_argument('--data_dir', type=str, default="")
    parser.add_argument('--dataset', type=str, default = 'tox21', help='root directory of dataset. For now, only classification.')
    parser.add_argument('--input_model_file', type=str, default='pretrained_model/MolGNet.pt',
                        help='filename to read the model (if there is any)')
    parser.add_argument('--exp', type=str, default = '', help='output filename')
    parser.add_argument('--seed', type=int, default=88, help = "Seed for splitting the dataset.")
    parser.add_argument('--runseed', type=int, default=0, help = "Seed for minibatch selection, random initialization.")
    parser.add_argument('--split', type = str, default="scaffold", help = "random or scaffold or random_scaffold")
    parser.add_argument('--eval_train', type=int, default = 0, help='evaluating training or not')
    parser.add_argument('--num_workers', type=int, default = 4, help='number of workers for dataset loading')
    parser.add_argument('--iters', type=int, default=10, help='number of run seeds')
    parser.add_argument('--cpu', default=False, action="store_true")
    args = parser.parse_args()

    device = torch.device("cuda:0") if torch.cuda.is_available() and not args.cpu else torch.device("cpu")
    print(device)

    for i in range(args.iters):
        seed=args.seed+i
        runseed=args.runseed
        torch.manual_seed(runseed)
        np.random.seed(runseed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(args.runseed)

        #Bunch of classification tasks
        if args.dataset == "tox21":
            num_tasks = 12
            args.batch_size=16
            args.lr = 0.0001
            args.lr_decay = 0.98
            args.dropout_ratio = 0.2
            args.graph_pooling = 'collection'
        elif args.dataset == "hiv":
            num_tasks = 1
        elif args.dataset == "muv":
            num_tasks = 17
        elif args.dataset == "bace":
            num_tasks = 1
            args.batch_size=16
            args.lr = 0.0001
            args.lr_decay = 0.99
            args.dropout_ratio = 0
            args.graph_pooling = 'collection'
            args.data= 'data/downstream/'
        elif args.dataset == "bbbp":
            num_tasks = 1
            args.batch_size=16
            args.lr = 0.00015
            args.lr_decay = 0.995
            args.dropout_ratio = 0.2
            args.graph_pooling = 'attention'
            args.data = 'data/downstream/'
        elif args.dataset == "toxcast":
            num_tasks = 617
            args.batch_size=16
            args.lr = 0.0001
            args.lr_decay = 0.98
            args.dropout_ratio = 0.2
            args.graph_pooling = 'collection'
        elif args.dataset == "sider":
            num_tasks = 27
            args.batch_size=16
            args.lr = 0.0001
            args.lr_decay = 0.995
            args.dropout_ratio = 0.2
            args.graph_pooling = 'collection'
        elif args.dataset == "clintox":
            num_tasks = 2
            args.batch_size=16
            args.lr = 0.0001
            args.lr_decay = 0.99
            args.dropout_ratio = 0.2
            args.graph_pooling='set2set'
            args.data = 'data/downstream/'
        else:
            raise ValueError("Invalid dataset name.")
        #set up dataset
        transform = Compose(
            [
             Self_loop(),Add_seg_id(),Add_collection_node(num_atom_type=119,bidirection=False)
            ]
        )
        dataset = MoleculeDataset(args.data_dir + args.dataset, dataset=args.dataset,transform=transform
        )

        smiles_list = pd.read_csv(args.data_dir + args.dataset + '/processed/smiles.csv')['smiles'].tolist()
        train_dataset, valid_dataset, test_dataset = random_scaffold_split(dataset, smiles_list, null_value=0,
                                                                           frac_train=0.8, frac_valid=0.1,
                                                                           frac_test=0.1,
                                                                           seed=seed)

        train_loader = DataLoaderMasking(train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers)
        val_loader = DataLoaderMasking(valid_dataset, batch_size=args.batch_size, shuffle=False, num_workers=args.num_workers)
        test_loader = DataLoaderMasking(test_dataset, batch_size=args.batch_size, shuffle=False, num_workers=args.num_workers)

        #set up model
        model = MolGT_graphpred(args.num_layer, args.emb_dim,args.heads,args.num_message_passing, num_tasks,
                              drop_ratio = args.dropout_ratio, graph_pooling = args.graph_pooling)
        if not args.input_model_file == "":
            model.from_pretrained(args.input_model_file)
            print('Pre-trained model loaded!')

        total = sum([param.nelement() for param in model.gnn.parameters()])
        print("Number of parameter: %.2fM" % (total / 1e6))

        model.to(device)

        #set up optimizer
        #different learning rate for different part of GNN
        model_param_group = []
        model_param_group.append({"params": model.gnn.parameters()})
        if args.graph_pooling == "attention":
            model_param_group.append({"params": model.pool.parameters(), "lr":args.lr*args.lr_scale})
        model_param_group.append({"params": model.graph_pred_linear.parameters(), "lr":args.lr*args.lr_scale})
        optimizer = optim.Adam(model_param_group, lr=args.lr, weight_decay=args.decay)

        scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer,gamma=args.lr_decay)
        criterion = nn.BCEWithLogitsLoss(reduction = "none")


        train_acc_list = []
        val_acc_list = []
        test_acc_list = []

        exp_path = '{}/{}_seed{}/'.format(args.exp,args.dataset,seed)
        if not os.path.exists(exp_path):
            os.makedirs(exp_path)

        best_acc = 0

        for epoch in range(1, args.epochs+1):
            print("====epoch " + str(epoch))
            train(args, model, device, train_loader, optimizer,criterion)
            scheduler.step()

            print("====Evaluation")
            train_acc = eval(args, model, device, train_loader)
            val_acc = eval(args, model, device, val_loader)
            test_acc = eval(args, model, device, test_loader)

            if val_acc>=best_acc:
                best_acc=val_acc
                torch.save(model.state_dict(), exp_path + "model_seed{}.pkl".format(args.seed))

            print("train: %f val: %f test: %f" %(train_acc, val_acc, test_acc))
            val_acc_list.append(val_acc)
            test_acc_list.append(test_acc)
            train_acc_list.append(train_acc)


        df = pd.DataFrame({'train':train_acc_list,'valid':val_acc_list,'test':test_acc_list})
        df.to_csv(exp_path+'{}_seed{}.csv'.format(args.dataset,seed))

        best_epoch = np.argmax(val_acc_list)
        test_acc_at_best_val = test_acc_list[best_epoch]
        print("The test auc at best valid (epoch {}) is {} at seed {}".format(best_epoch,test_acc_at_best_val,args.runseed))


    # if not args.filename == "":
    #     writer.close()

    # mean_score, std_score = np.nanmean(all_test_acc), np.nanstd(all_test_acc)
    #
    # logs = '{},{},{},{},{},{},{:.3f},{:.3f},{:.3f},{:.3f},{:.3f}'.format(
    #     args.dataset,args.lr,args.dropout_ratio,args.lr_decay,args.graph_pooling,args.epochs,
    #     all_test_acc[0],all_test_acc[1],all_test_acc[2],mean_score,std_score)
    # print(logs)
    # with open('runs/{}_log.csv'.format(args.dataset),'a+') as f:
    #     f.write('\n')
    #     f.write(logs)




if __name__ == "__main__":
    main()
