'''
CapsNet: A PyTorch implementation of Hinton et al's paper
Matrix Capsules with EM Routing (https://openreview.net/forum?id=HJWLfGWRb)

Code adapted from: https://github.com/shzygmyx/Matrix-Capsules-pytorch
'''

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import math
from torch.autograd import Variable

verbose = True

class PrimaryCaps(nn.Module):
    """
    Primary Capsule layer is nothing more than concatenate several convolutional
    layer together.
    Args:
        A:input channel
        B:number of types of capsules.

    """
    def __init__(self,A=32, B=32):
        super(PrimaryCaps, self).__init__()
        self.B = B
        self.capsules_pose = nn.ModuleList([nn.Conv2d(in_channels=A,out_channels=4*4,
                                                 kernel_size=1,stride=1)
                                                 for i in range(self.B)])
        self.capsules_activation = nn.ModuleList([nn.Conv2d(in_channels=A,out_channels=1,
                                                 kernel_size=1,stride=1) for i
                                                 in range(self.B)])

    def forward(self, x): #b,14,14,32
        poses = [self.capsules_pose[i](x) for i in range(self.B)]#(b,16,12,12) *32
        poses = torch.cat(poses, dim=1) #b,16*32,12,12
        activations = [self.capsules_activation[i](x) for i in range(self.B)] #(b,1,12,12)*32
        activations = F.sigmoid(torch.cat(activations, dim=1)) #b,32,12,12
        output = torch.cat([poses, activations], dim=1)
        return output

class ConvCaps(nn.Module):
    """
    Convolutional Capsule Layer.
    Args:
        B:input number of types of capsules.
        C:output number of types of capsules.
        kernel: kernel of convolution. kernel=0 means the capsules in layer L+1's
        receptive field contain all capsules in layer L. Kernel=0 is used in the
        final ClassCaps layer.
        stride:stride of convolution
        iteration: number of EM iterations
        coordinate_add: whether to use Coordinate Addition
        transform_share: whether to share transformation matrix.

    """
    def __init__(self, B=32, C=32, kernel = 3, stride=2,iteration=3,
                 coordinate_add=False, transform_share = False):
        super(ConvCaps, self).__init__()
        self.B =B
        self.C=C
        self.K=kernel # kernel = 0 means full receptive field like class capsules
        self.stride = stride
        self.coordinate_add=coordinate_add
        self.transform_share = transform_share
        self.beta_v = nn.Parameter(torch.randn(1))
        self.beta_a = nn.Parameter(torch.randn(C)) #TODO: make sure whether beta_a depend on c
        if not transform_share:
            self.W = nn.Parameter(torch.randn(self.B, kernel,kernel,self.C,
                                              4, 4)) #B,K,K,C,4,4
        else:
            self.W = nn.Parameter(torch.randn(self.B, self.C, 4, 4)) #B,C,4,4
        self.iteration=iteration

    def forward(self, x, lambda_,):
#        t = time()
        b = x.size(0) #batchsize
        width_in = x.size(2)  #12
        use_cuda = next(self.parameters()).is_cuda
        pose = x[:,:-self.B,:,:].contiguous() #b,16*32,12,12
        pose = pose.view(b,16,self.B,width_in,width_in).permute(0,2,3,4,1).contiguous() #b,B,12,12,16
        activation = x[:,-self.B:,:,:] #b,B,12,12
        w = width_out = int((width_in-self.K)/self.stride+1) if self.K else 1 #5
        if self.transform_share:
            if self.K == 0:
                self.K = width_in # class Capsules' kernel = width_in
            W = self.W.view(self.B,1,1,self.C,4,4).expand(self.B,self.K,self.K,self.C,4,4).contiguous()
        else:
            W = self.W #B,K,K,C,4,4

        #used to store every capsule i's poses in each capsule c's receptive field
        poses = torch.stack([pose[:,:,self.stride*i:self.stride*i+self.K,
                       self.stride*j:self.stride*j+self.K,:] for i in range(w) for j in range(w)], dim=-1) #b,B,K,K,w*w,16
        poses = poses.view(b,self.B,self.K,self.K,1,w,w,4,4) #b,B,K,K,1,w,w,4,4
        W_hat = W[None,:,:,:,:,None,None,:,:]                #1,B,K,K,C,1,1,4,4
        votes = torch.matmul(W_hat, poses) #b,B,K,K,C,w,w,4,4

        #Coordinate Addition
        add = [] #K,K,w,w
        if self.coordinate_add:
            for i in range(self.K):
                for j in range(self.K):
                    for x in range(w):
                        for y in range(w):
                            #compute where is the V_ic
                            pos_x = self.stride*x + i
                            pos_y = self.stride*y + j
                            add.append([pos_x/width_in, pos_y/width_in])
            add = Variable(torch.Tensor(add)).view(1,1,self.K,self.K,1,w,w,2)
            add = add.expand(b,self.B,self.K,self.K,self.C,w,w,2).contiguous()
            if use_cuda:
                add = add.cuda()
            votes[:,:,:,:,:,:,:,0,:2] = votes[:,:,:,:,:,:,:,0,:2] + add

#        print(time()-t)
        #Start EM
        Cww = w*w*self.C
        Bkk = self.K*self.K*self.B
        R = np.ones([b,self.B,width_in,width_in,self.C,w,w])/Cww
        V_s = votes.view(b,Bkk,Cww,16) #b,Bkk,Cww,16
        for iterate in range(self.iteration):
#            t = time()
            #M-step
            r_s,a_s = [],[]
            for typ in range(self.C):
                for i in range(width_out):
                    for j in range(width_out):
                        r = R[:,:,self.stride*i:self.stride*i+self.K,  #b,B,K,K
                                self.stride*j:self.stride*j+self.K,typ,i,j]
                        r = Variable(torch.from_numpy(r).float())
                        if use_cuda:
                            r = r.cuda()
                        r_s.append(r)
                        a = activation[:,:,self.stride*i:self.stride*i+self.K,
                                self.stride*j:self.stride*j+self.K] #b,B,K,K
                        a_s.append(a)


            r_s = torch.stack(r_s,-1).view(b, Bkk, Cww) #b,Bkk,Cww
            a_s = torch.stack(a_s,-1).view(b, Bkk, Cww) #b,Bkk,Cww
            r_hat = r_s*a_s #b,Bkk,Cww
            r_hat = r_hat.clamp(0.01) #prevent nan since we'll devide sth. by r_hat
            sum_r_hat = r_hat.sum(1).view(b,1,Cww,1).expand(b,1,Cww,16) #b,Cww,16
            r_hat_stack = r_hat.view(b,Bkk,Cww,1).expand(b, Bkk, Cww,16) #b,Bkk,Cww,16
            mu = torch.sum(r_hat_stack*V_s, 1, True)/sum_r_hat #b,1,Cww,16
            mu_stack = mu.expand(b,Bkk,Cww,16) #b,Bkk,Cww,16
            sigma = torch.sum(r_hat_stack*(V_s-mu_stack)**2,1,True)/sum_r_hat #b,1,Cww,16
            sigma = sigma.clamp(0.01) #prevent nan since the following is a log(sigma)
            cost = (self.beta_v + torch.log(sigma)) * sum_r_hat #b,1,Cww,16
            beta_a_stack = self.beta_a.view(1,self.C,1).expand(b,self.C,w*w).contiguous().view(b,1,Cww)#b,Cww
            a_c = torch.sigmoid(lambda_*(beta_a_stack-torch.sum(cost,3))) #b,1,Cww
            mus = mu.view(b,self.C,w,w,16) #b,C,w,w,16
            sigmas = sigma.view(b,self.C,w,w,16) #b,C,w,w,16
            activations = a_c.view(b,self.C,w,w) #b,C,w,w
#            print(time()-t)
#            t = time()

            #E-step
            for i in range(width_in):
                #compute the x axis range of capsules c that i connect to.
                x_range = (max(math.floor((i-self.K)/self.stride)+1,0),min(i//self.stride+1,width_out))
                #without padding, some capsules i may not be convolutional layer catched, in mnist case, i or j == 11
                u = len(range(*x_range))
                if not u:
                    continue
                for j in range(width_in):
                    y_range = (max(math.floor((j-self.K)/self.stride)+1,0),min(j//self.stride+1,width_out))

                    v = len(range(*y_range))
                    if not v:
                        continue
                    mu = mus[:,:,x_range[0]:x_range[1],y_range[0]:y_range[1],:].contiguous() #b,C,u,v,16
                    sigma = sigmas[:,:,x_range[0]:x_range[1],y_range[0]:y_range[1],:].contiguous() #b,C,u,v,16
                    mu = mu.view(b,1,self.C,u,v,16).expand(b,self.B,self.C,u,v,16).contiguous()#b,B,C,u,v,16
                    sigma = sigma.view(b,1,self.C,u,v,16).expand(b,self.B,self.C,u,v,16).contiguous()#b,B,C,u,v,16
                    V = []; a = []
                    for x in range(*x_range):
                        for y in range(*y_range):
                            #compute where is the V_ic
                            pos_x = self.stride*x - i
                            pos_y = self.stride*y - j
                            V.append(votes[:,:,pos_x,pos_y,:,x,y,:,:]) #b,B,C,4,4
                            a.append(activations[:,:,x,y].contiguous().view(b,1,self.C).expand(b,self.B,self.C).contiguous()) #b,B,C
                    V = torch.stack(V,dim=3).view(b,self.B,self.C,u,v,16) #b,B,C,u,v,16
                    a = torch.stack(a,dim=3).view(b,self.B,self.C,u,v) #b,B,C,u,v
                    p = torch.exp(-(V-mu)**2)/torch.sqrt(2*math.pi*sigma) #b,B,C,u,v,16
                    p = p.prod(dim=5)#b,B,C,u,v
                    p_hat = a*p  #b,B,C,u,v
                    sum_p_hat = p_hat.sum(4).sum(3).sum(2) #b,B
                    sum_p_hat = sum_p_hat.view(b,self.B,1,1,1).expand(b,self.B,self.C,u,v)
                    r = (p_hat/sum_p_hat) #b,B,C,u,v --> R: b,B,12,12,32,5,5

                    if use_cuda:
                        r = r.cpu()
                    R[:,:,i,j,:,x_range[0]:x_range[1],        #b,B,u,v,C
                      y_range[0]:y_range[1]] = r.data.numpy()
#            print(time()-t)

        mus = mus.permute(0,4,1,2,3).contiguous().view(b,self.C*16,w,w)#b,16*C,5,5
        output = torch.cat([mus,activations], 1) #b,C*17,5,5
        return output

class CapsNet(nn.Module):
    def __init__(self,A=32,B=32,C=32,D=32, E=10,r = 3):
        super(CapsNet, self).__init__()
        self.num_classes = E
        self.conv1 = nn.Conv2d(in_channels=3, out_channels=A,
                               kernel_size=5, stride=2)
        self.primary_caps = PrimaryCaps(A,B)
        self.convcaps1 = ConvCaps(B, C, kernel = 3, stride=2,iteration=r,
                                  coordinate_add=False, transform_share = False)
        self.convcaps2 = ConvCaps(C, D, kernel = 3, stride=1,iteration=r,
                                  coordinate_add=False, transform_share = False)
        self.classcaps = ConvCaps(D, E, kernel = 0, stride=1,iteration=r,
                                  coordinate_add=True, transform_share = True)


    def forward(self,x,lambda_): #b,1,28,28
        if verbose:
            print('Image Input')
            print(x.data.shape)
        x = F.relu(self.conv1(x)) #b,32,12,12
        if verbose:
            print('After conv1')
            print(x.data.shape)
        x = self.primary_caps(x) #b,32*(4*4+1),12,12
        if verbose:
            print('After Primary Caps')
            print(x.data.shape)
        x = self.convcaps1(x,lambda_) #b,32*(4*4+1),5,5
        if verbose:
            print('After ConvCaps1')
            print(x.data.shape)
        x = self.convcaps2(x,lambda_) #b,32*(4*4+1),3,3
        if verbose:
            print('After ConvCaps2')
            print(x.data.shape)
        x = self.classcaps(x,lambda_)
        if verbose:
            print('After ClassCaps')
            print(x.data.shape)
        x = x.view(-1,self.num_classes*16+self.num_classes) #b,10*16+10
        if verbose:
            print('After ClassCaps Reshape')
            print(x.data.shape)
        return x

    def loss(self, x, target, m): #x:b,10 target:b
        b = x.size(0)
        a_t = torch.cat([x[i][target[i]] for i in range(b)]) #b
        a_t_stack = a_t.view(b,1).expand(b,10).contiguous() #b,10
        u = m-(a_t_stack-x) #b,10
        mask = u.ge(0).float() #max(u,0) #b,10
        loss = ((mask*u)**2).sum()/b - m**2  #float
        return loss

    def loss2(self,x ,target):
        loss = F.cross_entropy(x,target)
        return loss
