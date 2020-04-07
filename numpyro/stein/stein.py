from functools import namedtuple
from numpyro import handlers
from numpyro.distributions import constraints
from numpyro.distributions.transforms import biject_to
from numpyro.infer.util import transform_fn, log_density
from .autoguides import AutoDelta
from ..util import ravel_pytree

import jax
import jax.random
import jax.numpy as np
from jax.tree_util import tree_map

SVGDState = namedtuple('SVGDState', ['optim_state', 'rng_key'])

# Lots of code based on SVI interface and commonalities should be refactored
class SVGD(object):
    """
    Stein Variational Gradient Descent for Non-parametric Inference.
    :param model: Python callable with Pyro primitives for the model.
    :param guide: Python callable with Pyro primitives for the guide
        (recognition network).
    :param optim: an instance of :class:`~numpyro.optim._NumpyroOptim`.
    :param num_stein_particles: number of particles for Stein inference.
        (More particles capture more of the posterior distribution)
    :param num_loss_particles: number of particles to evaluate the loss.
        (More loss particles reduce variance of loss estimates for each Stein particle)
    :param static_kwargs: static arguments for the model / guide, i.e. arguments
        that remain constant during fitting.
    """
    def __init__(self, model, guide, optim, log_kernel_fn, num_stein_particles=10, num_loss_particles=2, **static_kwargs):
        assert isinstance(guide, AutoDelta) # Only AutoDelta guide supported for now
        self.model = model
        self.guide = guide
        self.optim = optim
        self.log_kernel_fn = log_kernel_fn
        self.static_kwargs = static_kwargs
        self.num_stein_particles = num_stein_particles
        self.num_loss_particles = num_loss_particles
        self.guide_param_names = None
        self.constrain_fn = None

    def _svgd_loss_and_grads(self, rng_key, unconstr_params, *args, **kwargs):
        # 0. Separate model and guide parameters, since only guide parameters are updated using Stein
        model_uparams = {p: v for p, v in unconstr_params.items() if p not in self.guide_param_names}
        guide_uparams = {p: v for p, v in unconstr_params.items() if p in self.guide_param_names}
        # 1. Collect each guide parameter into monolithic particles that capture correlations between parameter values across each individual particle
        guide_particles, unravel_pytree = ravel_pytree(guide_uparams, batch_dims=1)
        # 2. Calculate log-joint-prob and gradients for each parameter (broadcasting by num_loss_particles for increased variance reduction)
        def log_joint_prob(rng_key, model_params, guide_params):
            params = {**model_params, **guide_params}
            def single_particle_ljp(rng_key):
                model_seed, guide_seed = jax.random.split(rng_key)
                seeded_model = handlers.seed(self.model, model_seed)
                seeded_guide = handlers.seed(self.guide, guide_seed)
                _, guide_trace = log_density(seeded_guide, args, kwargs, params)
                seeded_model = handlers.replay(seeded_model, guide_trace)
                model_log_density, _ = log_density(seeded_model, args, kwargs, params)
                return model_log_density
        
            if self.num_loss_particles == 1:
                return single_particle_ljp(rng_key)
            else:
                rng_keys = jax.random.split(rng_key, self.num_loss_particles)
                return np.mean(jax.vmap(single_particle_ljp)(rng_keys))
        jfp_fn = lambda rks, mps, gps: jax.vmap(lambda rk, gps: log_joint_prob(rk, mps, gps))(rks, gps)
        rng_keys = jax.random.split(rng_key, self.num_stein_particles)
        loss, particle_ljp_grads = jax.value_and_grad(lambda ps: jfp_fn(rng_keys, self.constrain_fn(model_uparams), self.constrain_fn(unravel_pytree(ps))))(guide_particles)
        model_param_grads = jax.grad(lambda mps: jfp_fn(rng_keys, self.constrain_fn(mps), self.constrain_fn(guide_uparams)))(model_uparams)
        # 3. Calculate kernel on monolithic particle
        log_kernel = self.log_kernel_fn(guide_particles)
        # 4. Calculate the attractive force and repulsive force on the monolithic particles
        attractive_force = jax.vmap(lambda x, x_ljp_grad: np.sum(jax.vmap(lambda y: log_kernel(x, y)*x_ljp_grad)(guide_particles), axis=0))(guide_particles, particle_ljp_grads)
        repulsive_force = jax.vmap(lambda x: np.sum(jax.vmap(lambda y: jax.grad(jax.partial(log_kernel, x))(y))(guide_particles), axis=0))(guide_particles)
        particle_grads = attractive_force + repulsive_force
        # 5. Decompose the monolithic particle forces back to concrete parameter values
        guide_param_grads = unravel_pytree(particle_grads)
        # 6. Return loss and gradients (based on parameter forces)
        res_grads = tree_map(lambda x: -x, {**model_param_grads, **guide_param_grads})
        return -loss, res_grads

    
    def init(self, rng_key, *args, **kwargs):
        """
        :param jax.random.PRNGKey rng_key: random number generator seed.
        :param args: arguments to the model / guide (these can possibly vary during
            the course of fitting).
        :param kwargs: keyword arguments to the model / guide (these can possibly vary
            during the course of fitting).
        :return: initial :data:`SVGDState`
        """
        rng_key, model_seed, guide_seed = jax.random.split(rng_key, 3)
        model_init = handlers.seed(self.model, model_seed)
        guide_init = handlers.seed(self.guide, guide_seed)
        guide_trace = handlers.trace(guide_init).get_trace(*args, **kwargs, **self.static_kwargs)
        model_trace = handlers.trace(model_init).get_trace(*args, **kwargs, **self.static_kwargs)
        rng_key, particle_seeds = jax.random.split(rng_key, 1 + self.num_stein_particles)
        self.guide.find_params(particle_seeds, *args, **kwargs, **self.static_kwargs) # Get parameter values for each particle
        params = {}
        inv_transforms = {}
        guide_param_names = set()
        # NB: params in model_trace will be overwritten by params in guide_trace
        for site in list(model_trace.values()) + list(guide_trace.values()):
            if site['type'] == 'param':
                constraint = site['kwargs'].pop('constraint', constraints.real)
                transform = biject_to(constraint)
                inv_transforms[site['name']] = transform
                pval = self.guide.init_params.get(site['name'], site['value'])
                params[site['name']] = transform.inv(pval)
                if site['name'] not in model_trace:
                    guide_param_names.update(site['name'])
        self.guide_param_names = guide_param_names
        self.constrain_fn = jax.partial(transform_fn, inv_transforms)
        return SVGDState(self.optim.init(params), rng_key)

    def get_params(self, state):
        """
        Gets values at `param` sites of the `model` and `guide`.
        :param svi_state: current state of the optimizer.
        """
        params = self.constrain_fn(self.optim.get_params(state.optim_state))
        return params

    def update(self, state, *args, **kwargs):
        """
        Take a single step of SVGD (possibly on a batch / minibatch of data),
        using the optimizer.
        :param state: current state of SVGD.
        :param args: arguments to the model / guide (these can possibly vary during
            the course of fitting).
        :param kwargs: keyword arguments to the model / guide (these can possibly vary
            during the course of fitting).
        :return: tuple of `(state, loss)`.
        """
        rng_key, rng_key_step = jax.random.split(state.rng_key)
        params = self.optim.get_params(state.optim_state)
        loss_val, grads = self._svgd_loss_and_grads(rng_key_step, params, 
                                                    *args, **kwargs, **self.static_kwargs)
        optim_state = self.optim.update(grads, state.optim_state)
        return SVGDState(optim_state, rng_key), loss_val

    def evaluate(self, state, *args, **kwargs):
        """
        Take a single step of SVGD (possibly on a batch / minibatch of data).
        :param state: current state of SVGD.
        :param args: arguments to the model / guide (these can possibly vary during
            the course of fitting).
        :param kwargs: keyword arguments to the model / guide.
        :return: evaluate loss given the current parameter values (held within `state.optim_state`).
        """
        # we split to have the same seed as `update_fn` given a state
        _, rng_key_eval = jax.random.split(state.rng_key)
        params = self.get_params(state)
        loss_val, _ = self._svgd_loss_and_grads(rng_key_eval, params, 
                                                *args, **kwargs, **self.static_kwargs)
        return loss_val