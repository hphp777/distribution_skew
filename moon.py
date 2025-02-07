import torch
import torch.nn.functional as F
from torch.nn.modules.loss import _Loss
from tqdm import tqdm
import numpy as np
from matplotlib import pyplot as plt
from pipelining import cifar100Loader, ChestXLoader, cifar10Loader
from torch.utils.data import DataLoader
from model.resnet import ResNet50_fedalign
from model.resnet import ResNet50
from model.efficientnet import EfficientNet
from typing import OrderedDict

class BMCLoss(_Loss):
    def __init__(self, init_noise_sigma = 8.):
        super(BMCLoss, self).__init__()
        self.noise_sigma = torch.nn.Parameter(torch.tensor(init_noise_sigma))

    def forward(self, pred, target):
        noise_var = self.noise_sigma ** 2
        return self.bmc_loss(pred, target, noise_var)

    def bmc_loss(self, pred, target, noise_var):
        """Compute the Balanced MSE Loss (BMC) between `pred` and the ground truth `targets`.
        Args:
        pred: A float tensor of size [batch, 1].
        target: A float tensor of size [batch, 1].
        noise_var: A float number or tensor.
        Returns:
        loss: A float tensor. Balanced MSE Loss.
        """

        logits = - (pred - target.T).pow(2) / (2 * noise_var)   # logit size: [batch, batch]
        loss = F.cross_entropy(logits, pred)     # contrastive-like loss
        loss = loss * (2 * noise_var).detach()  # optional: restore the loss scale, 'detach' when noise is learnable 

        return loss

class server():

    def __init__(self, model):
        self.model_name = model
        self.batch = 128
        self.device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
        # self.dataloader = DataLoader(cifar10Loader(), batch_size = self.batch, shuffle=True)
        self.width_range = [0.25, 1.0]
        
        # model
        if self.model_name == 'resnet':
            self.model = ResNet50.resnet56()
            self.model.to(self.device)
            self.model.apply(lambda m: setattr(m, 'width_mult', self.width_range[-1]))
        elif self.model_name == 'efficientnetb0':
            self.model = EfficientNet.efficientnet_b0()
            self.model.to(self.device)

    def merge_weight(self, weights, client_num, clients, total_data_num):
        
        weight = self.model.state_dict() # weight 안의 파라미터들이 변하기는 함
        cw = []
        for i in range(client_num):
            cw.append(len(clients[i].dataloader) / total_data_num)

        for key in weight:
            weight[key] = sum([weights[i][key] * cw[i] for i in range(client_num)])           

        return weight

    def test(self, weight, round):
        
        self.model.to(self.device)
        self.model.load_state_dict(weight)
        torch.save(self.model.state_dict(), './model/resnet/weight_FedAlign_CIFAR100/global_model_round' + str(round) + '.pth')
        self.model.eval()
        dataloader = DataLoader(ChestXLoader(mode = 'test'), batch_size = self.batch,shuffle=True) ###

        with torch.no_grad(): # for the evaluation mode
            
            self.model.apply(lambda m: setattr(m, 'width_mult', self.width_range[-1]))
            test_correct = 0.0 
            total = 0.0
            
            for _, (imgs, labels) in enumerate(tqdm(dataloader, desc= "Test Round")):

                imgs = imgs.to(self.device) # allocate data to the device
                labels = labels.cpu().detach().numpy()
                pred = self.model(imgs)
                pred = np.argmax(pred.cpu().detach().numpy(), axis=1)
                correct = (pred == labels).sum()
                test_correct += correct
                total += len(labels)
            acc = test_correct / total

        print("Global Test Accuracy : " , (test_correct / total) * 100)

        return acc

class client():
    def __init__(self, client_number , model):
        
        # hyperparameter
        self.epochs = 2 # local epochs
        self.learning_rate = 0.0001
        self.weight_decay = 0.01
        self.width_range = [0.25, 1.0]
        self.mu = 0.45
        self.cnum = client_number
        self.batch = 128
        self.model_name = model

        # model
        if self.model_name == 'resnet':
            self.model = ResNet50.resnet56(KD = True)
            self.global_model = ResNet50.resnet56(KD = True)
            self.prev_model = ResNet50.resnet56(KD = True)
        elif self.model_name == 'efficientnetb0':
            self.model = EfficientNet.efficientnet_b0()

        # train option
        self.bmse = BMCLoss()
        self.device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
        self.criterion = torch.nn.CrossEntropyLoss().to(self.device)
        self.optimizer = torch.optim.SGD(self.model.parameters(), lr=self.learning_rate, momentum=0.1, weight_decay=self.weight_decay, nesterov=True)
        # self.optimizer = torch.optim.RAdam(self.model.parameters(), lr = self.learning_rate, weight_decay=self.weight_decay)
        self.scheduler = torch.optim.lr_scheduler.StepLR(optimizer=self.optimizer, step_size=1, gamma=0.5)
        self.dataloader = DataLoader(ChestXLoader(self.cnum, mode = 'train'), batch_size = self.batch, shuffle=True) ### 
        self.cos = torch.nn.CosineSimilarity(dim=-1)
        self.temp = 0.5
        
    def train(self,updated = False, weight = None, t_r = None):
        
        self.updated = updated
        self.model.to(self.device) # allocate model to device
        self.model.train() # set model as train mode
        epoch_loss = []
        epoch_acc = []
        
        PATH = "./model.pt"
        grad_scaler = torch.cuda.amp.GradScaler(enabled=False)
        # self.model.load_state_dict(torch.load(PATH))

        # If I recieve the weight
        if self.updated == True:
            # print("loaded")
            self.model.load_state_dict(weight)
            self.global_model.load_state_dict(weight)

        print("-----client" + str(self.cnum) + "-----")
        for epoch in range(self.epochs):
            
            batch_loss = []
            total_correct = 0.0
            total_data = 0.0
            self.model.train()
            # print("client " + str(self.cnum) + " training")

            for batch_idx, (imgs, labels) in enumerate(tqdm(self.dataloader, desc="Training Epoch " + str(epoch+1) + "/" + str(self.epochs))):

                imgs = imgs.to(self.device) # allocate data to the device
                target = labels.clone().detach().type(torch.LongTensor).to(self.device)

                self.optimizer.zero_grad() # optimize the training process

                pro1, out = self.model(imgs) # [B x 32 x 16 x 16, B x 64 x 8 x 8], B x num_classes
                pro2, _ = self.global_model(imgs)

                posi = self.cos(pro1, pro2)
                logits = posi.reshape(-1,1)

                pro3, _ = self.prev_model(imgs)
                nega = self.cos(pro1, pro3)
                logits = torch.cat((logits, nega.reshape(-1,1)), dim=1)

                logits /= self.temp
                labels = torch.zeros(imgs.size(0)).to(self.device).long()

                loss2 = self.args.mu * self.criterion(logits, labels)

                loss1 = self.criterion(out, target)
                loss = loss1 + loss2
                grad_scaler.scale(loss).backward()


                # calculate accuracy
                teacher_output_n = np.argmax(out.cpu().detach().numpy(), axis=1)
                correct = (teacher_output_n == labels.cpu().detach().numpy()).sum()
                total_correct += correct
                total_data += labels.size(0)

                torch.nn.utils.clip_grad_norm_(self.model.parameters(), 10.0)
                grad_scaler.step(self.optimizer)
                grad_scaler.update()
                # self.optimizer.step()
                batch_loss.append(loss.item())
            # self.scheduler.step()
            acc = (total_correct / total_data) * 100
            print("Train Accuracy: ", acc)

            if len(batch_loss) > 0:
                epoch_loss.append(sum(batch_loss) / len(batch_loss))
                epoch_acc.append(acc)
        
        weights = self.model.state_dict()
        self.prev_model.load_state_dict(weights)
        return acc, weights # return weights as a result of the training

    def transmitting_matrix(self, fm1, fm2):
        if fm1.size(2) > fm2.size(2):
            fm1 = F.adaptive_avg_pool2d(fm1, (fm2.size(2), fm2.size(3)))

        fm1 = fm1.view(fm1.size(0), fm1.size(1), -1)
        fm2 = fm2.view(fm2.size(0), fm2.size(1), -1).transpose(1, 2)

        fsp = torch.bmm(fm1, fm2) / fm1.size(2)
        return fsp

    def top_eigenvalue(self, K, n_power_iterations=10, dim=1):
        v = torch.ones(K.shape[0], K.shape[1], 1).to(self.device)
        for _ in range(n_power_iterations):
            m = torch.bmm(K, v)
            n = torch.norm(m, dim=1).unsqueeze(1)
            v = m / n

        top_eigenvalue = torch.sqrt(n / torch.norm(v, dim=1).unsqueeze(1))
        return top_eigenvalue
    


    def bmc_loss(pred, target, noise_var):
        """Compute the Balanced MSE Loss (BMC) between `pred` and the ground truth `targets`.
        Args:
        pred: A float tensor of size [batch, 1].
        target: A float tensor of size [batch, 1].
        noise_var: A float number or tensor.
        Returns:
        loss: A float tensor. Balanced MSE Loss.
        """
        logits = - (pred - target.T).pow(2) / (2 * noise_var)   # logit size: [batch, batch]
        loss = F.cross_entropy(logits, torch.arange(pred.shape[0]))     # contrastive-like loss
        loss = loss * (2 * noise_var).detach()  # optional: restore the loss scale, 'detach' when noise is learnable 

        return loss

    def test(self, weight):

        self.model.load_state_dict(weight)
        self.model.eval()
        dataloader = DataLoader(cifar10Loader(mode = 'test'), batch_size = self.batch,shuffle=True)

        with torch.no_grad(): # for the evaluation mode
            
            self.model.apply(lambda m: setattr(m, 'width_mult', self.width_range[-1]))
            test_correct = 0.0 
            total = 0.0
            
            for _, (imgs, labels) in enumerate(tqdm(dataloader, desc= "Test Round")):

                imgs = imgs.to(self.device) # allocate data to the device
                labels = labels.cpu().detach().numpy()
                pred = self.model(imgs)
                pred = np.argmax(pred.cpu().detach().numpy(), axis=1)
                correct = (pred == labels).sum()
                test_correct += correct
                total += len(labels)

        print("Test Accuracy : " , (test_correct / total) * 100)