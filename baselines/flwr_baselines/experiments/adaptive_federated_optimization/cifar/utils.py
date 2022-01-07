from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset
from collections import OrderedDict
from flwr.common.parameter import weights_to_parameters
from flwr.common.typing import Parameters, Weights
from flwr.server.history import History
from flwr.dataset.utils.common import XY, create_lda_partitions
from torch.nn import GroupNorm, Module
from torchvision.datasets import CIFAR10, CIFAR100
from torchvision.models import ResNet, resnet18
from torchvision.transforms import Compose, Normalize, RandomHorizontalFlip, ToTensor
from typing import Callable, Dict, Optional, Tuple

# transforms
transform_cifar10_test = Compose(
    [ToTensor(), Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010))]
)
transform_cifar100_test = Compose(
    [ToTensor(), Normalize((0.5071, 0.4865, 0.4409), (0.2673, 0.2564, 0.2762))]
)
transform_cifar10_train = Compose([RandomHorizontalFlip(), transform_cifar10_test])


def get_model(num_classes: int = 10) -> Module:
    model: ResNet = resnet18(
        norm_layer=lambda x: GroupNorm(2, x), num_classes=num_classes
    )
    return model


def get_initial_parameters() -> Parameters:
    model = get_model()
    weights = [val.cpu().numpy() for _, val in model.state_dict().items()]
    parameters = weights_to_parameters(weights)

    return parameters


def partition_and_save(
    dataset: XY,
    fed_dir: Path,
    dirichlet_dist: np.ndarray = None,
    num_partitions: int = 500,
    concentration: float = 0.1,
) -> np.ndarray:
    # Create partitions
    clients_partitions, dist = create_lda_partitions(
        dataset=dataset,
        dirichlet_dist=dirichlet_dist,
        num_partitions=num_partitions,
        concentration=concentration,
    )
    # Save partions
    for idx, partition in enumerate(clients_partitions):
        path_dir = fed_dir / f"{idx}"
        path_dir.mkdir(exist_ok=True, parents=True)
        torch.save(partition, path_dir / "train.pt")

    return dist


def train(
    net: Module,
    trainloader: DataLoader,
    epochs: int,
    device: str,
    learning_rate: float = 0.01,
    momentum: float = 0.9,
) -> None:
    """Train the network on the training set."""
    criterion = torch.nn.CrossEntropyLoss()
    optimizer = torch.optim.SGD(net.parameters(), lr=learning_rate, momentum=momentum)
    net.train()
    for _ in range(epochs):
        for images, labels in trainloader:
            images, labels = images.to(device), labels.to(device)
            optimizer.zero_grad()
            loss = criterion(net(images), labels)
            loss.backward()
            optimizer.step()


def test(net: Module, testloader: DataLoader, device: str) -> Tuple[float, float]:
    """Validate the network on the entire test set."""
    criterion = torch.nn.CrossEntropyLoss()
    correct, total, loss = 0, 0, 0.0
    net.eval()
    with torch.no_grad():
        for data in testloader:
            images, labels = data[0].to(device), data[1].to(device)
            outputs = net(images)
            loss += criterion(outputs, labels).item()
            _, predicted = torch.max(outputs.data, 1)
            total += labels.size(0)
            correct += (predicted == labels).sum().item()
    accuracy = correct / total
    return loss, accuracy


def get_cifar_eval_fn(
    path_original_dataset: Path, num_classes: int = 10
) -> Callable[[Weights], Optional[Tuple[float, Dict[str, float]]]]:
    """Returns an evaluation function for centralized evaluation."""
    if num_classes == 10:
        CIFAR = CIFAR10
        transform = transform_cifar10_test
    else:
        CIFAR = CIFAR100
        transform = transform_cifar100_test

    testset = CIFAR(
        root=path_original_dataset,
        train=False,
        download=True,
        transform=transform,
    )

    def evaluate(weights: Weights) -> Optional[Tuple[float, Dict[str, float]]]:
        """Use the entire CIFAR-10 test set for evaluation."""

        # determine device
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        net = get_model()
        state_dict = OrderedDict(
            {
                k: torch.tensor(np.atleast_1d(v))
                for k, v in zip(net.state_dict().keys(), weights)
            }
        )
        net.load_state_dict(state_dict, strict=True)
        net.to(device)

        testloader = torch.utils.data.DataLoader(testset, batch_size=50)
        loss, accuracy = test(net, testloader, device=device)

        # return statistics
        return loss, {"accuracy": accuracy}

    return evaluate


def gen_cifar10_partitions(
    path_original_dataset: Path,
    fed_dir: Path,
    num_total_clients: int,
    lda_concentration: float,
) -> None:

    trainset = CIFAR10(root=path_original_dataset, train=True, download=True)
    flwr_trainset = (trainset.data, np.array(trainset.targets, dtype=np.int32))
    partition_and_save(
        dataset=flwr_trainset,
        fed_dir=fed_dir,
        dirichlet_dist=None,
        num_partitions=num_total_clients,
        concentration=lda_concentration,
    )


def gen_fit_config_fn(
    epochs_per_round: int, batch_size: int, client_learning_rate: float
):
    def fit_config(rnd: int) -> Dict[str, str]:
        """Return a configuration with specific client learning rate."""
        local_config = {
            "epoch_global": str(rnd),
            "epochs": str(epochs_per_round),
            "batch_size": str(batch_size),
            "client_learning_rate": str(client_learning_rate),
        }
        return local_config


def plot_metric_from_history(
    hist: History,
    metric_str: str,
    strategy_name: str,
    expected_maximum: float,
    save_path: Path,
):
    x, y = zip(*hist.metrics_centralized[metric_str])
    plt.plot(x, y * 100)  # Accuracy 0-100%
    # Set expected graph
    plt.axhline(y=expected_maximum, color="r", linestyle="--")
    plt.title(f"Centralized Validation - {strategy_name}")
    plt.xlabel("Rounds")
    plt.ylabel("Accuracy")
    plt.savefig(save_path)
