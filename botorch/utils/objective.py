#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

r"""
Helpers for handling objectives.
"""

from __future__ import annotations

import warnings

from typing import Callable, List, Optional, Union

import torch
from botorch.utils.safe_math import log_fatmoid, logexpit
from torch import Tensor


def get_objective_weights_transform(
    weights: Optional[Tensor],
) -> Callable[[Tensor, Optional[Tensor]], Tensor]:
    r"""Create a linear objective callable from a set of weights.

    Create a callable mapping a Tensor of size `b x q x m` and an (optional)
    Tensor of size `b x q x d` to a Tensor of size `b x q`, where `m` is the
    number of outputs of the model using scalarization via the objective weights.
    This callable supports broadcasting (e.g. for calling on a tensor of shape
    `mc_samples x b x q x m`). For `m = 1`, the objective weight is used to
    determine the optimization direction.

    Args:
        weights: a 1-dimensional Tensor containing a weight for each task.
            If not provided, the identity mapping is used.

    Returns:
        Transform function using the objective weights.

    Example:
        >>> weights = torch.tensor([0.75, 0.25])
        >>> transform = get_objective_weights_transform(weights)
    """
    # if no weights provided, just extract the single output
    if weights is None:
        return lambda Y: Y.squeeze(-1)

    def _objective(Y: Tensor, X: Optional[Tensor] = None):
        r"""Evaluate objective.

        Note: einsum multiples Y by weights and sums over the `m`-dimension.
        Einsum is ~2x faster than using `(Y * weights.view(1, 1, -1)).sum(dim-1)`.

        Args:
            Y: A `... x b x q x m` tensor of function values.

        Returns:
            A `... x b x q`-dim tensor of objective values.
        """
        return torch.einsum("...m, m", [Y, weights])

    return _objective


def apply_constraints_nonnegative_soft(
    obj: Tensor,
    constraints: List[Callable[[Tensor], Tensor]],
    samples: Tensor,
    eta: Union[Tensor, float],
) -> Tensor:
    r"""Applies constraints to a non-negative objective.

    This function uses a sigmoid approximation to an indicator function for
    each constraint.

    Args:
        obj: A `n_samples x b x q (x m')`-dim Tensor of objective values.
        constraints: A list of callables, each mapping a Tensor of size `b x q x m`
            to a Tensor of size `b x q`, where negative values imply feasibility.
            This callable must support broadcasting. Only relevant for multi-
            output models (`m` > 1).
        samples: A `n_samples x b x q x m` Tensor of samples drawn from the posterior.
        eta: The temperature parameter for the sigmoid function. Can be either a float
            or a 1-dim tensor. In case of a float the same eta is used for every
            constraint in constraints. In case of a tensor the length of the tensor
            must match the number of provided constraints. The i-th constraint is
            then estimated with the i-th eta value.

    Returns:
        A `n_samples x b x q (x m')`-dim tensor of feasibility-weighted objectives.
    """
    w = compute_smoothed_feasibility_indicator(
        constraints=constraints, samples=samples, eta=eta
    )
    if obj.dim() == samples.dim():
        w = w.unsqueeze(-1)  # Need to unsqueeze to accommodate the outcome dimension.
    return obj.clamp_min(0).mul(w)  # Enforce non-negativity of obj, apply constraints.


def compute_feasibility_indicator(
    constraints: Optional[List[Callable[[Tensor], Tensor]]],
    samples: Tensor,
) -> Tensor:
    r"""Computes the feasibility of a list of constraints given posterior samples.

    Args:
        constraints: A list of callables, each mapping a batch_shape x q x m`-dim Tensor
            to a `batch_shape x q`-dim Tensor, where negative values imply feasibility.
        samples: A batch_shape x q x m`-dim Tensor of posterior samples.

    Returns:
        A `batch_shape x q`-dim tensor of Boolean feasibility values.
    """
    ind = torch.ones(samples.shape[:-1], dtype=torch.bool, device=samples.device)
    if constraints is not None:
        for constraint in constraints:
            ind = ind.logical_and(constraint(samples) < 0)
    return ind


def compute_smoothed_feasibility_indicator(
    constraints: List[Callable[[Tensor], Tensor]],
    samples: Tensor,
    eta: Union[Tensor, float],
    log: bool = False,
    fat: bool = False,
) -> Tensor:
    r"""Computes the smoothed feasibility indicator of a list of constraints.

    Given posterior samples, using a sigmoid to smoothly approximate the feasibility
    indicator of each individual constraint to ensure differentiability and high
    gradient signal. The `fat` and `log` options improve the numerical behavior of
    the smooth approximation.

    NOTE: *Negative* constraint values are associated with feasibility.

    Args:
        constraints: A list of callables, each mapping a Tensor of size `b x q x m`
            to a Tensor of size `b x q`, where negative values imply feasibility.
            This callable must support broadcasting. Only relevant for multi-
            output models (`m` > 1).
        samples: A `n_samples x b x q x m` Tensor of samples drawn from the posterior.
        eta: The temperature parameter for the sigmoid function. Can be either a float
            or a 1-dim tensor. In case of a float the same eta is used for every
            constraint in constraints. In case of a tensor the length of the tensor
            must match the number of provided constraints. The i-th constraint is
            then estimated with the i-th eta value.
        log: Toggles the computation of the log-feasibility indicator.
        fat: Toggles the computation of the fat-tailed feasibility indicator.

    Returns:
        A `n_samples x b x q`-dim tensor of feasibility indicator values.
    """
    if type(eta) != Tensor:
        eta = torch.full((len(constraints),), eta)
    if len(eta) != len(constraints):
        raise ValueError(
            "Number of provided constraints and number of provided etas do not match."
        )
    if not (eta > 0).all():
        raise ValueError("eta must be positive.")
    is_feasible = torch.zeros_like(samples[..., 0])
    log_sigmoid = log_fatmoid if fat else logexpit
    for constraint, e in zip(constraints, eta):
        is_feasible = is_feasible + log_sigmoid(-constraint(samples) / e)

    return is_feasible if log else is_feasible.exp()


# TODO: deprecate this function
def soft_eval_constraint(lhs: Tensor, eta: float = 1e-3) -> Tensor:
    r"""Element-wise evaluation of a constraint in a 'soft' fashion

    `value(x) = 1 / (1 + exp(x / eta))`

    Args:
        lhs: The left hand side of the constraint `lhs <= 0`.
        eta: The temperature parameter of the softmax function. As eta
            decreases, this approximates the Heaviside step function.

    Returns:
        Element-wise 'soft' feasibility indicator of the same shape as `lhs`.
        For each element `x`, `value(x) -> 0` as `x` becomes positive, and
        `value(x) -> 1` as x becomes negative.
    """
    warnings.warn(
        "`soft_eval_constraint` is deprecated. Please consider `torch.utils.sigmoid` "
        + "with its `fat` and `log` options to compute feasibility indicators.",
        DeprecationWarning,
    )
    if eta <= 0:
        raise ValueError("eta must be positive.")
    return torch.sigmoid(-lhs / eta)


def apply_constraints(
    obj: Tensor,
    constraints: List[Callable[[Tensor], Tensor]],
    samples: Tensor,
    infeasible_cost: float,
    eta: Union[Tensor, float] = 1e-3,
) -> Tensor:
    r"""Apply constraints using an infeasible_cost `M` for negative objectives.

    This allows feasibility-weighting an objective for the case where the
    objective can be negative by using the following strategy:
    (1) Add `M` to make obj non-negative;
    (2) Apply constraints using the sigmoid approximation;
    (3) Shift by `-M`.

    Args:
        obj: A `n_samples x b x q (x m')`-dim Tensor of objective values.
        constraints: A list of callables, each mapping a Tensor of size `b x q x m`
            to a Tensor of size `b x q`, where negative values imply feasibility.
            This callable must support broadcasting. Only relevant for multi-
            output models (`m` > 1).
        samples: A `n_samples x b x q x m` Tensor of samples drawn from the posterior.
        infeasible_cost: The infeasible value.
        eta: The temperature parameter of the sigmoid function. Can be either a float
            or a 1-dim tensor. In case of a float the same eta is used for every
            constraint in constraints. In case of a tensor the length of the tensor
            must match the number of provided constraints. The i-th constraint is
            then estimated with the i-th eta value.

    Returns:
        A `n_samples x b x q (x m')`-dim tensor of feasibility-weighted objectives.
    """
    # obj has dimensions n_samples x b x q (x m')
    obj = obj.add(infeasible_cost)  # now it is nonnegative
    obj = apply_constraints_nonnegative_soft(
        obj=obj,
        constraints=constraints,
        samples=samples,
        eta=eta,
    )
    return obj.add(-infeasible_cost)
