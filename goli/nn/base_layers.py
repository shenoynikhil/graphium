from copy import deepcopy
import torch
import torch.nn as nn
import torch.nn.functional as F

from typing import Union, Callable, Optional, Type

SUPPORTED_ACTIVATION_MAP = {"ReLU", "Sigmoid", "Tanh", "ELU", "SELU", "GLU", "LeakyReLU", "Softplus", "None"}


def get_activation(activation: Union[type(None), str, Callable]) -> Optional[Callable]:
    r"""
    returns the activation function represented by the input string

    Parameters:
        activation: Callable, `None`, or string with value:
            "none", "ReLU", "Sigmoid", "Tanh", "ELU", "SELU", "GLU", "LeakyReLU", "Softplus"

    Returns:
        Callable or None: The activation function
    """
    if (activation is not None) and callable(activation):
        # activation is already a function
        return activation

    if (activation is None) or (activation.lower() == "none"):
        return None

    # search in SUPPORTED_ACTIVATION_MAP a torch.nn.modules.activation
    activation = [x for x in SUPPORTED_ACTIVATION_MAP if activation.lower() == x.lower()]
    assert len(activation) == 1 and isinstance(activation[0], str), "Unhandled activation function"
    activation = activation[0]

    return vars(torch.nn.modules.activation)[activation]()


def get_norm(self, normalization: Union[Type[None], str, Callable]):
    r"""
    returns the normalization function represented by the input string

    Parameters:
        parsed_norm: Callable, `None`, or string with value:
            "none", "batch_norm", "layer_norm"

    Returns:
        Callable or None: The normalization function
    """
    parsed_norm = None
    if normalization is None or normalization == "none":
        pass
    elif callable(normalization):
        parsed_norm = normalization
    elif normalization == "batch_norm":
        parsed_norm = nn.BatchNorm1d(self.out_dim)
    elif normalization == "layer_norm":
        parsed_norm = nn.LayerNorm(self.out_dim)
    else:
        raise ValueError(
            f"Undefined normalization `{normalization}`, must be `None`, `Callable`, 'batch_norm', 'layer_norm', 'none'"
        )
    return deepcopy(parsed_norm)


class FCLayer(nn.Module):
    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        activation: Union[str, Callable] = "relu",
        dropout: float = 0.0,
        normalization: Union[str, Callable] = "none",
        bias: bool = True,
        init_fn: Optional[Callable] = None,
    ):

        r"""
        A simple fully connected and customizable layer. This layer is centered around a torch.nn.Linear module.
        The order in which transformations are applied is:

        - Dense Layer
        - Activation
        - Dropout (if applicable)
        - Batch Normalization (if applicable)

        Parameters:
            in_dim:
                Input dimension of the layer (the torch.nn.Linear)
            out_dim:
                Output dimension of the layer.
            dropout:
                The ratio of units to dropout. No dropout by default.
            activation:
                Activation function to use.
            normalization:
                Normalization to use. Choices:

                - "none" or `None`: No normalization
                - "batch_norm": Batch normalization
                - "layer_norm": Layer normalization
                - `Callable`: Any callable function
            bias:
                Whether to enable bias in for the linear layer.
            init_fn:
                Initialization function to use for the weight of the layer. Default is
                $$\mathcal{U}(-\sqrt{k}, \sqrt{k})$$ with $$k=\frac{1}{ \text{in_dim}}$$

        Attributes:
            dropout (int):
                The ratio of units to dropout.
            normalization (None or Callable):
                Normalization layer
            linear (torch.nn.Linear):
                The linear layer
            activation (torch.nn.Module):
                The activation layer
            init_fn (Callable):
                Initialization function used for the weight of the layer
            in_dim (int):
                Input dimension of the linear layer
            out_dim (int):
                Output dimension of the linear layer
        """

        super().__init__()

        self.__params = locals()
        del self.__params["__class__"]
        del self.__params["self"]
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.bias = bias
        self.linear = nn.Linear(in_dim, out_dim, bias=bias)
        self.dropout = None
        self.normalization = self._parse_norm(normalization)

        if dropout:
            self.dropout = nn.Dropout(p=dropout)
        self.activation = get_activation(activation)
        self.init_fn = init_fn if init_fn is not None else nn.init.xavier_uniform_

        self.reset_parameters()

    def reset_parameters(self, init_fn=None):
        init_fn = init_fn or self.init_fn
        if init_fn is not None:
            init_fn(self.linear.weight, 1 / self.in_dim)
        if self.bias:
            self.linear.bias.data.zero_()

    def forward(self, h: torch.Tensor) -> torch.Tensor:
        r"""
        Apply the FC layer on the input features.

        Parameters:

            h: `torch.Tensor[..., Din]`:
                Input feature tensor, before the FC.
                `Din` is the number of input features

        Returns:

            `torch.Tensor[..., Dout]`:
                Output feature tensor, after the FC.
                `Dout` is the number of output features

        """

        if torch.prod(torch.as_tensor(h.shape[:-1])) == 0:
            h = torch.zeros(list(h.shape[:-1]) + [self.linear.out_features], device=h.device, dtype=h.dtype)
            return h

        h = self.linear(h)

        if self.normalization is not None:
            if h.shape[1] != self.out_dim:
                h = self.normalization(h.transpose(1, 2)).transpose(1, 2)
            else:
                h = self.normalization(h)

        if self.dropout is not None:
            h = self.dropout(h)
        if self.activation is not None:
            h = self.activation(h)

        return h

    @property
    def in_channels(self) -> int:
        r"""
        Get the input channel size. For compatibility with PyG.
        """
        return self.in_dim

    @property
    def out_channels(self) -> int:
        r"""
        Get the output channel size. For compatibility with PyG.
        """
        return self.out_dim

    def __repr__(self):
        return f"{self.__class__.__name__}({self.in_dim} -> {self.out_dim}, activation={self.activation})"


class MLP(nn.Module):
    def __init__(
        self,
        in_dim: int,
        hidden_dim: int,
        out_dim: int,
        layers: int,
        activation: Union[str, Callable] = "relu",
        last_activation: Union[str, Callable] = "none",
        dropout=0.0,
        normalization="none",
        last_normalization="none",
        first_normalization="none",
        last_dropout=0.0,
    ):
        r"""
        Simple multi-layer perceptron, built of a series of FCLayers

        Parameters:
            in_dim:
                Input dimension of the MLP
            hidden_dim:
                Hidden dimension of the MLP. All hidden dimensions will have
                the same number of parameters
            out_dim:
                Output dimension of the MLP.
            layers:
                Number of hidden layers
            activation:
                Activation function to use in all the layers except the last.
                if `layers==1`, this parameter is ignored
            last_activation:
                Activation function to use in the last layer.
            dropout:
                The ratio of units to dropout. Must be between 0 and 1
            normalization:
                Normalization to use. Choices:

                - "none" or `None`: No normalization
                - "batch_norm": Batch normalization
                - "layer_norm": Layer normalization in the hidden layers.
                - `Callable`: Any callable function

                if `layers==1`, this parameter is ignored
            last_normalization:
                Norrmalization to use **after the last layer**. Same options as `normalization`.
            first_normalization:
                Norrmalization to use in **before the first layer**. Same options as `normalization`.
            last_dropout:
                The ratio of units to dropout at the last layer.

        """

        super().__init__()

        self.in_dim = in_dim
        self.hidden_dim = hidden_dim
        self.out_dim = out_dim
        self.first_normalization = get_norm(first_normalization)

        self.fully_connected = nn.Sequential()
        if layers <= 1:
            self.fully_connected.append(
                FCLayer(
                    in_dim,
                    out_dim,
                    activation=last_activation,
                    normalization=last_normalization,
                    dropout=last_dropout,
                )
            )
        else:
            self.fully_connected.append(
                FCLayer(
                    in_dim,
                    hidden_dim,
                    activation=activation,
                    normalization=normalization,
                    dropout=dropout,
                )
            )
            for _ in range(layers - 2):
                self.fully_connected.append(
                    FCLayer(
                        hidden_dim,
                        hidden_dim,
                        activation=activation,
                        normalization=normalization,
                        dropout=dropout,
                    )
                )
            self.fully_connected.append(
                FCLayer(
                    hidden_dim,
                    out_dim,
                    activation=last_activation,
                    normalization=last_normalization,
                    dropout=last_dropout,
                )
            )

    def forward(self, h: torch.Tensor) -> torch.Tensor:
        r"""
        Apply the MLP on the input features.

        Parameters:

            h: `torch.Tensor[..., Din]`:
                Input feature tensor, before the MLP.
                `Din` is the number of input features

        Returns:

            `torch.Tensor[..., Dout]`:
                Output feature tensor, after the MLP.
                `Dout` is the number of output features

        """
        if self.first_normalization is not None:
            h = self.first_normalization(h)
        h = self.fully_connected(h)
        return h

    def __getitem__(self, idx: int) -> nn.Module:
        return self.fully_connected[idx]

    def __repr__(self):
        r"""
        Controls how the class is printed
        """
        return self.__class__.__name__ + " (" + str(self.in_dim) + " -> " + str(self.out_dim) + ")"


class GRU(nn.Module):
    def __init__(self, in_dim: int, hidden_dim: int):
        r"""
        Wrapper class for the GRU used by the GNN framework, nn.GRU is used for the Gated Recurrent Unit itself

        Parameters:
            in_dim:
                Input dimension of the GRU layer
            hidden_dim:
                Hidden dimension of the GRU layer.
        """

        super().__init__()
        self.in_dim = in_dim
        self.hidden_dim = hidden_dim
        self.gru = nn.GRU(in_dim=in_dim, hidden_dim=hidden_dim)

    def forward(self, x, y):
        r"""
        Parameters:
            x:  `torch.Tensor[B, N, Din]`
                where Din <= in_dim (difference is padded)
            y:  `torch.Tensor[B, N, Dh]`
                where Dh <= hidden_dim (difference is padded)

        Returns:
            torch.Tensor: `torch.Tensor[B, N, Dh]`

        """
        assert x.shape[-1] <= self.in_dim and y.shape[-1] <= self.hidden_dim

        (B, N, _) = x.shape
        x = x.reshape(1, B * N, -1).contiguous()
        y = y.reshape(1, B * N, -1).contiguous()

        # padding if necessary
        if x.shape[-1] < self.in_dim:
            x = F.pad(input=x, pad=[0, self.in_dim - x.shape[-1]], mode="constant", value=0)
        if y.shape[-1] < self.hidden_dim:
            y = F.pad(input=y, pad=[0, self.hidden_dim - y.shape[-1]], mode="constant", value=0)

        x = self.gru(x, y)[1]
        x = x.reshape(B, N, -1)
        return x
