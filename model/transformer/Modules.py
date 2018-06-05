import torch
import torch.nn as nn
import torch.nn.init as init
import numpy as np

from global_random_seed import RANDOM_SEED
np.random.seed(RANDOM_SEED)


class Linear(nn.Module):
    ''' Simple Linear layer with xavier init '''
    def __init__(self, d_in, d_out, bias=True):
        super(Linear, self).__init__()
        self.linear = nn.Linear(d_in, d_out, bias=bias)
        init.xavier_normal_(self.linear.weight)

    def forward(self, x):
        return self.linear(x)


class Bottle(nn.Module):
    ''' Perform the reshape routine before and after an operation '''

    def forward(self, input):
        if len(input.size()) <= 2:
            return super(Bottle, self).forward(input)

        size = input.size()[:2]
        out = super(Bottle, self).forward(input.view(size[0]*size[1], -1))
        return out.view(size[0], size[1], -1)


class BottleLinear(Bottle, Linear):
    ''' Perform the reshape routine before and after a linear projection '''
    pass


class BottleSoftmax(Bottle, nn.Softmax):
    ''' Perform the reshape routine before and after a softmax operation'''
    pass


class LayerNormalization(nn.Module):
    ''' Layer normalization module '''

    def __init__(self, d_hid, eps=1e-3):
        super(LayerNormalization, self).__init__()

        self.eps = eps
        self.a_2 = nn.Parameter(torch.ones(d_hid), requires_grad=True)
        self.b_2 = nn.Parameter(torch.zeros(d_hid), requires_grad=True)

    def forward(self, z):
        if z.size(1) == 1:
            return z

        mu = torch.mean(z, keepdim=True, dim=-1)
        sigma = torch.std(z, keepdim=True, dim=-1)
        ln_out = (z - mu.expand_as(z)) / (sigma.expand_as(z) + self.eps)
        ln_out = ln_out * self.a_2.expand_as(ln_out) + self.b_2.expand_as(ln_out)

        return ln_out


class BatchBottle(nn.Module):
    ''' Perform the reshape routine before and after an operation '''

    def forward(self, input):
        if len(input.size()) <= 2:
            return super(BatchBottle, self).forward(input)
        size = input.size()[1:]
        out = super(BatchBottle, self).forward(input.view(-1, size[0]*size[1]))
        return out.view(-1, size[0], size[1])


class BottleLayerNormalization(BatchBottle, LayerNormalization):
    ''' Perform the reshape routine before and after a layer normalization'''
    pass


class ScaledDotProductAttention(nn.Module):
    ''' Scaled Dot-Product Attention '''

    def __init__(self, d_model, attn_dropout=0.1, temper_value=0.5):
        super(ScaledDotProductAttention, self).__init__()

        # add temper as hyperparameter
        self.temper = np.power(d_model, temper_value)    # 0.5 originally
        self.dropout = nn.Dropout(attn_dropout)
        self.softmax = BottleSoftmax(dim=-1)  # ? -1

    def forward(self, q, k, v, attn_mask=None, position_dpa=None):

        # initial attention
        attn = torch.bmm(q, k.transpose(1, 2)) / self.temper

        verbose_sizes = False

        # work with diagonal positional encodings
        if position_dpa is not None:
            if verbose_sizes:
                print("using diagonal positional encodings 2")
                print()
                print("q.size()                    ", q.size())                             # [150, 86, 120]
                print("k.transpose(1, 2).size()    ", k.transpose(1, 2).size())             # [150, 120, 86]
                print("attn.size()                 ", attn.size())                          # [150, 86, 86]
                print("position_dpa.size()         ", position_dpa.size())                  # [150, 86, 120]
                print("position_dpa.transpose(1, 2)", position_dpa.transpose(1, 2).size())  # [150, 120, 86]
                print()

            # TODO: do we include temper here as well?
            attn_pos = torch.bmm(q, position_dpa.transpose(1, 2)) / self.temper

            # apply mask to the diagonal positional attention as well
            if verbose_sizes:
                print(attn_pos.size())   # [150, 86, 86]

            def batch_stripe(a):
                """
                Get a diagonal stripe of a matrix m x n, where n > m
                this implementation also takes into account batched matrices,
                so the stripe is calculated over a batch x for a matrix of size[x, m, n]
                """
                # another solution
                # a = a[::-1]  # ValueError: negative step not yet supported
                # do the usual left top to right bottom
                # return a[::-1]

                b, i, j = a.size()
                assert i > j
                b_s, k, l = a.stride()

                # left top to right bottom
                return torch.as_strided(a, (b, i - j, j), (b_s, k, k + 1))

                # left bottom to right top
                # a = a[..., j-1:, :]
                # return torch.as_strided(a, (b, i-j, j), (b_s, k, l-k))

            def flip_old(x, dim):
                """ Flip matrix """

                # TODO: follow the official release of optimized flip:
                # https://github.com/pytorch/pytorch/pull/7873

                dim = x.dim() + dim if dim < 0 else dim
                indices = [slice(None)] * x.dim()
                indices[dim] = torch.arange(x.size(dim) - 1, -1, -1, dtype=torch.long, device="cuda")
                return x[tuple(indices)]

            def multi_meshgrid(*args):
                """
                Creates a meshgrid from possibly many
                elements (instead of only 2).
                Returns a nd tensor with as many dimensions
                as there are arguments
                """
                args = list(args)
                template = [1 for _ in args]
                for i in range(len(args)):
                    n = args[i].shape[0]
                    template_copy = template.copy()
                    template_copy[i] = n
                    args[i] = args[i].view(*template_copy)
                    # there will be some broadcast magic going on
                return tuple(args)

            def flip(tensor, dims):
                """
                This function should be in native PyTorch hopefully after 0.4
                :param tensor:
                :param dims:
                :return:
                """
                if not isinstance(dims, (tuple, list)):
                    dims = [dims]
                indices = [torch.arange(tensor.shape[dim] - 1, -1, -1,
                                        dtype=torch.long, device="cuda") for dim in dims]
                multi_indices = multi_meshgrid(*indices)
                final_indices = [slice(i) for i in tensor.shape]
                for i, dim in enumerate(dims):
                    final_indices[dim] = multi_indices[i]
                flipped = tensor[final_indices]

                # TODO
                # need to permute the final dimensions
                # if dims is not consecutive

                return flipped

            # print(attn_pos.transpose(1, 2).dim())

            # left top to right bottom
            # dim=-1
            # attn_pos = batch_stripe(attn_pos.transpose(1, 2))

            # left bottom to right top
            # dim=-1
            attn_pos = batch_stripe(flip(attn_pos.transpose(1, 2), -1))

            # print(attn_pos.size())

            if verbose_sizes:
                print(attn_pos.size())
                print(attn.size())
                print(attn_pos.transpose(1, 2).size())

            attn = attn + attn_pos.transpose(1, 2)

            # print(attn.size())

        if attn_mask is not None:
            # print(attn_mask)
            # print(attn_mask.size(), attn.size())

            assert attn_mask.size() == attn.size(), \
                    'Attention mask shape {} mismatch ' \
                    'with Attention logit tensor shape ' \
                    '{}.'.format(attn_mask.size(), attn.size())

            attn.data.masked_fill_(attn_mask, -float('inf'))

        # print(attn.size())

        attn = self.softmax(attn)
        attn = self.dropout(attn)

        output = torch.bmm(attn, v)

        return output, attn
