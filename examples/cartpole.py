import atexit
import numpy as np
import torch
import tensorboardX

from functools import partial

from kusanagi.shell import cartpole
from kusanagi.base import ExperienceDataset, apply_controller
from kusanagi.ghost.control import RandPolicy

from prob_mbrl import utils, models, algorithms, losses, train_regressor
torch.set_flush_denormal(True)
torch.set_num_threads(4)


def forward(states, actions, dynamics, **kwargs):
    deltas, rewards = dynamics(
        (states, actions),
        return_samples=True,
        separate_outputs=True,
        **kwargs)
    return states + deltas, rewards


def reward_fn(states, target, Q, angle_dims):
    states = utils.to_complex(states, angle_dims)
    return -losses.quadratic_saturating_loss(states, target, Q)


if __name__ == '__main__':
    # parameters
    n_rnd = 1
    H = 25
    N_particles = 100
    dyn_components = 4
    dyn_hidden = [200] * 2
    pol_hidden = [50] * 2
    use_cuda = False
    learn_reward = False
    target = torch.tensor([0, 0, 0, np.pi]).float()
    maxU = np.array([10.0])
    angle_dims = torch.tensor([3]).long()

    # initialize environment
    env = cartpole.Cartpole(friction=1.0)

    # initialize reward/cost function
    D = target.shape[-1]
    U = 1
    target = utils.to_complex(target, angle_dims)
    Da = target.shape[-1]
    Q = torch.zeros(Da, Da).float()
    Q[0, 0] = 1
    Q[0, -2] = env.l
    Q[-2, 0] = env.l
    Q[-2, -2] = env.l**2
    Q[-1, -1] = env.l**2
    Q /= 0.1
    if learn_reward:
        reward_func = None
    else:
        reward_func = partial(
            reward_fn, target=target, Q=Q, angle_dims=angle_dims)

    # initialize dynamics model
    dynE = 2 * (D + 1) if learn_reward else 2 * D
    dyn_model = models.dropout_mlp(
        Da + U, (dynE + 1) * dyn_components,
        dyn_hidden,
        dropout_layers=[
            models.modules.CDropout(0.1, 0.1) for i in range(len(dyn_hidden))
        ],
        nonlin=torch.nn.ReLU)
    dyn = models.DynamicsModel(
        dyn_model,
        reward_func=reward_func,
        angle_dims=angle_dims,
        output_density=models.MixtureDensity(dynE / 2,
                                             dyn_components)).float()

    # initalize policy
    pol_model = models.dropout_mlp(
        Da,
        U,
        pol_hidden,
        dropout_layers=[
            models.modules.BDropout(0.1) for i in range(len(pol_hidden))
        ],
        nonlin=torch.nn.Tanh,
        weights_initializer=torch.nn.init.xavier_normal_,
        biases_initializer=None,
        output_nonlin=torch.nn.Tanh)

    pol = models.Policy(pol_model, maxU, angle_dims=angle_dims).float()
    randpol = RandPolicy(maxU)

    # initalize experience dataset
    exp = ExperienceDataset()

    # initialize dynamics optimizer
    opt1 = torch.optim.Adam(dyn.parameters(), 1e-4)

    # initialize policy optimizer
    opt2 = torch.optim.Adam(pol.parameters(), 1e-3)

    # define functions required for rollouts
    forward_fn = partial(forward, dynamics=dyn)

    # collect initial random experience
    for rand_it in range(n_rnd):
        ret = apply_controller(
            env, randpol, H,
            callback=None)  # lambda *args, **kwargs: env.render())
        exp.append_episode(*ret)

    if use_cuda and torch.cuda.is_available():
        dyn = dyn.cuda()
        pol = pol.cuda()

    writer = tensorboardX.SummaryWriter()
    writer.add_scalar('robot/evaluation_loss', torch.tensor(ret[2]).sum(), 0)

    def on_close():
        writer.close()

    atexit.register(on_close)

    # policy learning loop
    for ps_it in range(100):

        def on_iteration(i, loss, states, actions, rewards, opt, policy,
                         dynamics):
            writer.add_scalar('mc_pilco/episode_%d/training loss' % ps_it,
                              loss, i)
            if i % 100 == 0:
                states = states.transpose(0, 1).cpu().detach().numpy()
                actions = actions.transpose(0, 1).cpu().detach().numpy()
                rewards = rewards.transpose(0, 1).cpu().detach().numpy()
                utils.plot_trajectories(
                    states, actions, rewards, plot_samples=False)

        # train dynamics
        X, Y = exp.get_dynmodel_dataset(deltas=True, return_costs=learn_reward)
        dyn.set_dataset(
            torch.tensor(X).to(dyn.X.device).float(),
            torch.tensor(Y).to(dyn.X.device).float())
        train_regressor(
            dyn,
            2000,
            N_particles,
            True,
            opt1,
            log_likelihood=losses.gaussian_mixture_log_likelihood)

        # sample initial states for policy optimization
        x0 = torch.tensor(exp.sample_states(N_particles, timestep=0)).to(
            dyn.X.device).float()
        x0 += 1e-1 * x0.std(0) * torch.randn_like(x0)
        utils.plot_rollout(x0, forward_fn, pol, H)

        # train policy
        print "Policy search iteration %d" % (ps_it + 1)
        algorithms.mc_pilco(
            x0,
            forward_fn,
            dyn,
            pol,
            H,
            opt2,
            exp,
            1000,
            pegasus=True,
            mm_states=True,
            mm_rewards=False,
            maximize=True,
            clip_grad=1.0,
            mpc=False,
            max_steps=25,
            on_iteration=on_iteration)
        utils.plot_rollout(x0, forward_fn, pol, H)

        # apply policy
        ret = apply_controller(env, pol, H, callback=None)
        exp.append_episode(*ret)
        writer.add_scalar('robot/evaluation_loss',
                          torch.tensor(ret[2]).sum(), ps_it + 1)
