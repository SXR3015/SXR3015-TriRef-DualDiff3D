import math
import torch
from torch import nn
from torch.nn import functional as F
from einops.layers.torch import Rearrange
# from scipy.ndimage import zoom
from torch.nn.functional import interpolate
from models.model._attention import Attention


def get_downsample_layer(in_dim, hidden_dim, is_last):
    if not is_last:
        return nn.Sequential(
            Rearrange('b c (h p1) (w p2) (d p3)-> b (c p1 p2 p3) h w d', p1=2, p2=2, p3=2),
            nn.Conv3d(in_dim * 8, hidden_dim, 1))
    else:
        return nn.Conv3d(in_dim, hidden_dim, 3, padding=1)


def get_attn_layer(in_dim, use_full_attn, use_flash_attn):
    if use_full_attn:
        return Attention(in_dim, use_flash_attn=use_flash_attn)
    else:
        return nn.Identity()

def skip_upsample_layer(in_dim, hidden_dim, is_last):
    if not is_last:
        return nn.Sequential(nn.Upsample(scale_factor=2, mode='nearest'),
                             nn.Conv3d(in_dim, hidden_dim, 3, padding=1))
    else:
        return nn.Conv3d(in_dim, hidden_dim, 3, padding=1)
def get_upsample_layer(in_dim, hidden_dim, is_last):
    if not is_last:
        return nn.Sequential(nn.Upsample(scale_factor=2, mode='nearest'),
                             nn.Conv3d(in_dim, hidden_dim, 3, padding=1))
    else:
        return nn.Conv3d(in_dim, hidden_dim, 3, padding=1)

def get_upsample_layer_fmri2dti(in_dim, hidden_dim, is_last):
        if not is_last:
            return nn.Sequential(nn.Upsample(scale_factor=2, mode='nearest'),
                                 nn.Conv3d(in_dim, hidden_dim, 3, padding=1))
        else:
            return nn.Sequential(nn.Upsample(scale_factor=1.5, mode='nearest'),
                                 nn.Conv3d(in_dim, hidden_dim, 3, padding=1))
        # if not is_last:
        #     return nn.Sequential(nn.Upsample(scale_factor=2, mode='nearest'),
        #                          nn.Conv3d(in_dim, hidden_dim, 3, padding=1))
        # else:
        #     return nn.Conv3d(in_dim, hidden_dim, 3, padding=1)


def get_upsample_layer_dti2fmri(in_dim, hidden_dim, is_last):
    if not is_last:
        return nn.Sequential(nn.Upsample(scale_factor=1.39, mode='nearest'),
                             nn.Conv3d(in_dim, hidden_dim, 3, padding=1))
    else:
        return nn.Sequential(nn.Upsample(scale_factor=1.39, mode='nearest'),
                             nn.Conv3d(in_dim, hidden_dim, 3, padding=1))
    # if not is_last:
    #     return nn.Sequential(nn.Upsample(scale_factor=2, mode='nearest'),
    #                          nn.Conv3d(in_dim, hidden_dim, 3, padding=1))
    # else:
    #     return nn.Conv3d(in_dim, hidden_dim, 3, padding=1)

def sinusoidal_embedding(timesteps, dim):
    half_dim = dim // 2
    exponent = -math.log(10000) * torch.arange(
        start=0, end=half_dim, dtype=torch.float32)
    exponent = exponent / (half_dim - 1.0)

    emb = torch.exp(exponent).to(device=timesteps.device)
    emb = timesteps[:, None].float() * emb[None, :]

    return torch.cat([emb.sin(), emb.cos()], dim=-1)

class ResidualBlock(nn.Module):

    def __init__(self,
                 in_channels,
                 out_channels,
                 temb_channels,
                 kernel_size=3,
                 stride=1,
                 padding=1,
                 groups=8):
        super(ResidualBlock, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels

        self.time_emb_proj = nn.Sequential(
            nn.SiLU(), torch.nn.Linear(temb_channels, out_channels))

        self.residual_conv = nn.Conv3d(
            in_channels, out_channels=out_channels,
            kernel_size=1) if in_channels != out_channels else nn.Identity()

        self.conv1 = nn.Conv3d(in_channels,
                               out_channels=out_channels,
                               kernel_size=kernel_size,
                               stride=stride,
                               padding=padding)
        self.conv2 = nn.Conv3d(out_channels,
                               out_channels=out_channels,
                               kernel_size=kernel_size,
                               stride=stride,
                               padding=padding)

        self.norm1 = nn.GroupNorm(num_channels=out_channels, num_groups=groups)
        self.norm2 = nn.GroupNorm(num_channels=out_channels, num_groups=groups)
        self.nonlinearity = nn.SiLU()

    def forward(self, x, temb):
        residual = self.residual_conv(x)

        x = self.conv1(x)
        x = self.norm1(x)
        x = self.nonlinearity(x)

        temb = self.time_emb_proj(self.nonlinearity(temb))
        x += temb[:, :, None, None,None]

        x = self.conv2(x)
        x = self.norm2(x)
        x = self.nonlinearity(x)

        return x + residual


class UNet(nn.Module):

    def __init__(self,
                 in_channels,
                 hidden_dims=[64, 128, 256, 512],
                 image_size_fmri=[64,64,36],
                 image_size_dti=[96, 96, 60],
                 use_flash_attn=False):
        super(UNet, self).__init__()

        self.sample_size = image_size_fmri
        self.in_channels = in_channels
        self.hidden_dims = hidden_dims
        self.fmri_size = image_size_fmri
        self.dti_size = image_size_dti

        timestep_input_dim = hidden_dims[0]
        time_embed_dim = timestep_input_dim * 4

        self.time_embedding = nn.Sequential(
            nn.Linear(timestep_input_dim, time_embed_dim), nn.SiLU(),
            nn.Linear(time_embed_dim, time_embed_dim))

        self.init_conv_d2f_resour = nn.Conv3d(in_channels,
                                   out_channels=hidden_dims[0],
                                   kernel_size=3,
                                   stride=1,
                                   padding=1)
        self.init_conv_d2f_dist = nn.Conv3d(in_channels,
                                   out_channels=hidden_dims[0],
                                   kernel_size=3,
                                   stride=1,
                                   padding=1)
        self.init_conv_f2d_dist  = nn.Conv3d(in_channels,
                                   out_channels=hidden_dims[0],
                                   kernel_size=3,
                                   stride=1,
                                   padding=1)
        self.init_conv_f2d_resour = nn.Conv3d(in_channels,
                                   out_channels=hidden_dims[0],
                                   kernel_size=3,
                                   stride=1,
                                   padding=1)

        down_blocks_fmri2dti = []

        in_dim = hidden_dims[0]
        for idx, hidden_dim in enumerate(hidden_dims[1:]):
            is_last = idx >= (len(hidden_dims) - 2)
            is_first = idx == 0
            use_attn = True if use_flash_attn else not is_first
            down_blocks_fmri2dti.append(
                nn.ModuleList([
                    ResidualBlock(in_dim, in_dim, time_embed_dim),
                    ResidualBlock(in_dim, in_dim, time_embed_dim),
                    get_attn_layer(in_dim, use_attn, use_flash_attn),
                    get_downsample_layer(in_dim, hidden_dim, is_last)
                ]))
            in_dim = hidden_dim

        self.down_blocks_fmri2dti = nn.ModuleList(down_blocks_fmri2dti)
        down_blocks_dti2fmri= []

        in_dim = hidden_dims[0]
        for idx, hidden_dim in enumerate(hidden_dims[1:]):
            is_last = idx >= (len(hidden_dims) - 2)
            is_first = idx == 0
            use_attn = True if use_flash_attn else not is_first
            down_blocks_dti2fmri.append(
                nn.ModuleList([
                    ResidualBlock(in_dim, in_dim, time_embed_dim),
                    ResidualBlock(in_dim, in_dim, time_embed_dim),
                    get_attn_layer(in_dim, use_attn, use_flash_attn),
                    get_downsample_layer(in_dim, hidden_dim, is_last)
                ]))
            in_dim = hidden_dim

        self.down_blocks_dti2fmri = nn.ModuleList(down_blocks_dti2fmri)

        mid_dim = hidden_dims[-1]
        self.mid_block1_fmri2dti = ResidualBlock(mid_dim, mid_dim, time_embed_dim)
        self.mid_attn_fmri2dti  = Attention(mid_dim)
        self.mid_block2_fmri2dti  = ResidualBlock(mid_dim, mid_dim, time_embed_dim)

        mid_dim = hidden_dims[-1]
        self.mid_block1_dti2fmri  = ResidualBlock(mid_dim, mid_dim, time_embed_dim)
        self.mid_attn_dti2fmri = Attention(mid_dim)
        self.mid_block2_dti2fmri = ResidualBlock(mid_dim, mid_dim, time_embed_dim)

        up_blocks_fmri2dti = []
        in_dim = mid_dim
        for idx, hidden_dim in enumerate(list(reversed(hidden_dims[:-1]))):
            is_last = idx >= (len(hidden_dims) - 2)
            use_attn = True if use_flash_attn else not is_last
            up_blocks_fmri2dti.append(
                nn.ModuleList([
                    ResidualBlock(in_dim + hidden_dim, in_dim, time_embed_dim),
                    ResidualBlock(in_dim + hidden_dim, in_dim, time_embed_dim),
                    get_attn_layer(in_dim, use_attn, use_flash_attn),
                    get_upsample_layer_fmri2dti(in_dim, hidden_dim, is_last)
                ]))
            in_dim = hidden_dim

        self.up_blocks_fmri2dti = nn.ModuleList(up_blocks_fmri2dti)

        up_blocks_dti2fmri = []
        in_dim = mid_dim
        for idx, hidden_dim in enumerate(list(reversed(hidden_dims[:-1]))):
            is_last = idx >= (len(hidden_dims) - 2)
            use_attn = True if use_flash_attn else not is_last
            up_blocks_dti2fmri.append(
                nn.ModuleList([
                    ResidualBlock(in_dim + hidden_dim, in_dim, time_embed_dim),
                    ResidualBlock(in_dim + hidden_dim, in_dim, time_embed_dim),
                    get_attn_layer(in_dim, use_attn, use_flash_attn),
                    get_upsample_layer_dti2fmri(in_dim, hidden_dim, is_last)
                ]))
            in_dim = hidden_dim

        self.up_blocks_dti2fmri = nn.ModuleList(up_blocks_dti2fmri)

        # self.out_block_f2d = ResidualBlock(hidden_dims[0] * 2, hidden_dims[0],
        #                                time_embed_dim)
        self.out_block_f2d = ResidualBlock(hidden_dims[0] * 2, hidden_dims[0],
                                       time_embed_dim)
        self.conv_out_f2d = nn.Conv3d(hidden_dims[0], out_channels=1, kernel_size=1)
        self.out_block_d2f = ResidualBlock(hidden_dims[0] * 2, hidden_dims[0],
                                       time_embed_dim)
        self.conv_out_d2f = nn.Conv3d(hidden_dims[0], out_channels=1, kernel_size=1)
    def forward(self, sample_resour, sample_dist, timesteps):
        if not torch.is_tensor(timesteps):
            timesteps = torch.tensor([timesteps],
                                     dtype=torch.long,
                                     device=sample_resour.device)

        timesteps = torch.flatten(timesteps)
        timesteps = timesteps.broadcast_to(sample_resour.shape[0])

        t_emb = sinusoidal_embedding(timesteps, self.hidden_dims[0])
        t_emb = self.time_embedding(t_emb)
        input_shape = [sample_resour.shape[2], sample_resour.shape[3], sample_resour.shape[4]]
        if input_shape == self.fmri_size:
            sample_resour = sample_resour
            sample_dist = sample_dist
        else: # if first input is not fMRI, change position to calculate their output, the following code is fMRI first.
            tmp = sample_dist
            sample_dist  = sample_resour
            sample_resour = tmp

        # u-net for fmri2dti
        skips_fmri2dti = []
        x_f2d = self.init_conv_f2d_resour(sample_resour)
        x_dist_f2d = self.init_conv_f2d_dist(sample_dist)
        for block1, block2, attn, downsample in self.down_blocks_fmri2dti:
            x_f2d = block1(x_f2d, t_emb)
            skips_fmri2dti.append(x_f2d)

            x_f2d = block2(x_f2d, t_emb)
            x_f2d = attn(x_f2d)
            skips_fmri2dti.append(x_f2d)

            x_f2d = downsample(x_f2d)

        x_f2d = self.mid_block1_fmri2dti(x_f2d, t_emb)
        x_f2d = self.mid_attn_fmri2dti(x_f2d)
        x_f2d = self.mid_block2_fmri2dti(x_f2d, t_emb)

        for block1, block2, attn, upsample in self.up_blocks_fmri2dti:
            skip_resize = interpolate(skips_fmri2dti.pop(), [x_f2d.shape[2], x_f2d.shape[3], x_f2d.shape[4]])
            x_f2d = torch.cat((x_f2d, skip_resize), dim=1)
            x_f2d = block1(x_f2d, t_emb)
            skip_resize = interpolate(skips_fmri2dti.pop(), [x_f2d.shape[2], x_f2d.shape[3], x_f2d.shape[4]])
            x_f2d = torch.cat((x_f2d, skip_resize), dim=1)
            x_f2d = block2(x_f2d, t_emb)
            x_f2d = attn(x_f2d)

            x_f2d = upsample(x_f2d)
        x_resize_f2d = interpolate(x_f2d, [self.dti_size[0], self.dti_size[1], self.dti_size[2]])
        r_resize_f2d = interpolate(x_dist_f2d, [self.dti_size[0], self.dti_size[1], self.dti_size[2]])
        x_f2d = self.out_block_f2d(torch.cat((x_resize_f2d, r_resize_f2d), dim=1), t_emb)
        out_dti = self.conv_out_f2d(x_f2d)

        # u-net for dti2fmri

        skips_dti2fmri = []
        x_d2f = self.init_conv_d2f_resour(sample_dist)
        x_dist_d2f = self.init_conv_d2f_dist(sample_resour)
        for block1, block2, attn, downsample in self.down_blocks_dti2fmri:
            x_d2f = block1(x_d2f, t_emb)
            skips_dti2fmri.append(x_d2f)

            x_d2f = block2(x_d2f, t_emb)
            x_d2f = attn(x_d2f)
            skips_dti2fmri.append(x_d2f)

            x_d2f = downsample(x_d2f)

        x_d2f = self.mid_block1_dti2fmri(x_d2f, t_emb)
        x_d2f = self.mid_attn_dti2fmri(x_d2f)
        x_d2f = self.mid_block2_dti2fmri(x_d2f, t_emb)

        for block1, block2, attn, upsample in self.up_blocks_dti2fmri:
            # channel = skips_dti2fmri.pop().shape[1]
            skip_resize = interpolate( skips_dti2fmri.pop(), [x_d2f.shape[2], x_d2f.shape[3],x_d2f.shape[4]])
            x_d2f = torch.cat((x_d2f, skip_resize), dim=1)
            x_d2f = block1(x_d2f, t_emb)
            skip_resize = interpolate(skips_dti2fmri.pop(), [x_d2f.shape[2], x_d2f.shape[3], x_d2f.shape[4]])
            x_d2f = torch.cat((x_d2f, skip_resize), dim=1)
            x_d2f = block2(x_d2f, t_emb)
            x_d2f = attn(x_d2f)

            x_d2f = upsample(x_d2f)
            if x_d2f.shape[2] > self.fmri_size[0]: # reconstructed fMRI resolution larger than soure fMRI, break
               break
        x_resize_d2f = interpolate(x_d2f, [self.fmri_size[0], self.fmri_size[1], self.fmri_size[2]])
        r_resize_d2f = interpolate(x_dist_d2f, [self.fmri_size[0], self.fmri_size[1], self.fmri_size[2]])
        x_d2f = self.out_block_d2f(torch.cat((x_resize_d2f, r_resize_d2f), dim=1), t_emb)
        out_fmri = self.conv_out_d2f(x_d2f)
        return {"sample_dti": out_dti, "sample_fmri": out_fmri}
