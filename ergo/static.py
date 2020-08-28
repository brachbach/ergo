from functools import partial

from jax import grad, jit, vmap
import jax.numpy as np
import jax.scipy as scipy

# Multi-condition loss, jitting entire function (used for logistic
# mixture dist + histogram loss)


@partial(jit, static_argnums=(0, 3))
def jitted_condition_loss(
    dist_class, dist_fixed_params, dist_opt_params, cond_classes, cond_params
):
    print(
        f"Tracing {dist_class.__name__} ({dist_fixed_params}) loss for {[c[0].__name__ for c in cond_classes]} ({str(cond_params)[:60]})"
    )
    dist = dist_class.from_params(dist_fixed_params, dist_opt_params, traceable=True)
    total_loss = 0.0
    for (cond_class, cond_param) in zip(cond_classes, cond_params):
        condition = cond_class[0].structure((cond_class, cond_param))
        total_loss += condition.loss(dist)
    return total_loss * 100


jitted_condition_loss_grad = jit(
    grad(jitted_condition_loss, argnums=2), static_argnums=(0, 3)
)


# Multi-condition loss, jitting only individual condition losses (used
# for histogram dist + arbitrary losses)


def condition_loss(
    dist_class, dist_fixed_params, dist_opt_params, cond_classes, cond_params
):
    total_loss = 0.0
    dist = get_dist(dist_class, dist_fixed_params, dist_opt_params)
    for (cond_class, cond_param) in zip(cond_classes, cond_params):
        total_loss += single_condition_loss(
            dist, cond_class, cond_param
        )
    return total_loss


def condition_loss_grad(
    dist_class, dist_fixed_params, dist_opt_params, cond_classes, cond_params
):
    total_grad = 0.0
    dist = get_dist(dist_class, dist_fixed_params, dist_opt_params)
    for (cond_class, cond_param) in zip(cond_classes, cond_params):
        total_grad += single_condition_loss_grad(
            dist, cond_class, cond_param
        )
    return total_grad

@partial(jit, static_argnums=(0))
def get_dist(dist_class, dist_fixed_params, dist_opt_params):
    return dist_class.from_params(dist_fixed_params, dist_opt_params, traceable=True)

@partial(jit, static_argnums=(1))
def single_condition_loss(
    dist, cond_class, cond_param
):
    condition = cond_class[0].structure((cond_class, cond_param))
    loss = condition.loss(dist) * 100
    print(
        f"Tracing {cond_class[0].__name__} loss for {dist_class.__name__} distribution:\n"
        f"- Fixed: {dist_fixed_params}\n"
        f"- Optim: {dist_opt_params}\n"
        f"- Cond: {cond_param}\n"
        f"- Loss: {loss}\n\n"
    )
    return loss


single_condition_loss_grad = jit(
    grad(single_condition_loss, argnums=2), static_argnums=( 1)
)


# Description of distribution/condition fit


@partial(jit, static_argnums=(0, 2))
def describe_fit(dist_classes, dist_params, cond_class, cond_params):
    dist_class = dist_classes[0]
    dist = dist_class.structure((dist_classes, dist_params))
    condition = cond_class[0].structure((cond_class, cond_params))
    return condition._describe_fit(dist)


# General negative log likelihood


@partial(jit, static_argnums=0)
def dist_logloss(dist_class, fixed_params, opt_params, data):
    dist = dist_class.from_params(fixed_params, opt_params, traceable=True)
    if data.size == 1:
        return -dist.logpdf(data)
    scores = vmap(dist.logpdf)(data)
    return -np.sum(scores)


dist_grad_logloss = jit(grad(dist_logloss, argnums=2), static_argnums=0)


# Logistic mixture


@jit
def logistic_logpdf(x, loc, scale):
    # x, loc, scale are assumed to be normalized
    # https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.logistic.html
    y = (x - loc) / scale
    return scipy.stats.logistic.logpdf(y) - np.log(scale)


@jit
def logistic_mixture_logpdf(params, data):
    # params are assumed to be normalized
    if data.size == 1:
        return logistic_mixture_logpdf1(params, data)
    scores = vmap(partial(logistic_mixture_logpdf1, params))(data)
    return np.sum(scores)


@jit
def logistic_mixture_logpdf1(params, datum):
    # params are assumed to be normalized
    structured_params = params.reshape((-1, 3))
    component_scores = []
    for (loc, scale, component_prob) in structured_params:
        component_scores.append(
            logistic_logpdf(datum, loc, scale) + np.log(component_prob)
        )
    return scipy.special.logsumexp(np.array(component_scores))


logistic_mixture_grad_logpdf = jit(grad(logistic_mixture_logpdf, argnums=0))


# Wasserstein distance


@jit
def wasserstein_distance(xs, ys):
    diffs = np.cumsum(xs - ys)
    abs_diffs = np.abs(diffs)
    return np.sum(abs_diffs)
