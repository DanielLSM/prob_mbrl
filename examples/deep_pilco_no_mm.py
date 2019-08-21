import atexit
import datetime
import numpy as np
import os
import torch
import tensorboardX

from prob_mbrl import utils, models, algorithms, envs
from functools import partial
torch.set_flush_denormal(True)
torch.set_num_threads(2)
torch.manual_seed(1)
np.random.seed(1)

if __name__ == '__main__':
    # parameters
    n_initial_epi = 1
    pred_H = 25
    control_H = 40
    dyn_batch_size = 100
    pol_batch_size = 100
    N_polopt = 1000
    N_dynopt = 2000
    N_ps = 250
    dyn_components = 1
    dyn_hidden = [200] * 2
    pol_hidden = [200] * 2
    use_cuda = False
    learn_reward = False
    keep_best = False

    # initialize environment
    env = envs.Cartpole()

    results_filename = os.path.expanduser(
        "~/.prob_mbrl/results_%s_%s.pth.tar" %
        (env.__class__.__name__,
         datetime.datetime.now().strftime("%Y%m%d%H%M%S.%f")))
    D = env.observation_space.shape[0]
    U = env.action_space.shape[0]
    maxU = env.action_space.high
    minU = env.action_space.low

    # initialize reward/cost function
    if learn_reward or env.reward_func is None:
        reward_func = None
    else:
        reward_func = env.reward_func

    # intialize to max episode steps if available
    if hasattr(env, 'spec'):
        if hasattr(env.spec, 'max_episode_steps'):
            control_H = env.spec.max_episode_steps
    initial_experience = control_H * n_initial_epi

    # initialize dynamics model
    dynE = 2 * (D + 1) if learn_reward else 2 * D
    if dyn_components > 1:
        output_density = models.GaussianMixtureDensity(dynE / 2,
                                                       dyn_components)
        dynE = (dynE + 1) * dyn_components + 1
    else:
        output_density = models.DiagGaussianDensity(dynE / 2)

    dyn_model = models.mlp(D + U,
                           dynE,
                           dyn_hidden,
                           dropout_layers=[
                               models.modules.CDropout(0.1, 0.1)
                               for i in range(len(dyn_hidden))
                           ],
                           nonlin=torch.nn.ReLU)
    dyn = models.DynamicsModel(dyn_model,
                               reward_func=reward_func,
                               output_density=output_density).float()

    # initalize policy
    pol_model = models.mlp(D,
                           2 * U,
                           pol_hidden,
                           dropout_layers=[
                               models.modules.BDropout(0.1)
                               for i in range(len(pol_hidden))
                           ],
                           nonlin=torch.nn.ReLU,
                           output_nonlin=partial(models.DiagGaussianDensity,
                                                 U))

    pol = models.Policy(pol_model, maxU, minU).float()

    print('Dynamics model\n', dyn)
    print('Policy\n', pol)

    # initalize experience dataset
    exp = utils.ExperienceDataset()

    # initialize dynamics optimizer
    opt1 = torch.optim.Adam(dyn.parameters(), 1e-4)

    # initialize policy optimizer
    opt2 = torch.optim.Adam(pol.parameters(), 1e-4)

    if use_cuda and torch.cuda.is_available():
        dyn = dyn.cuda()
        pol = pol.cuda()

    writer = tensorboardX.SummaryWriter()

    # callbacks
    def on_close():
        writer.close()

    atexit.register(on_close)

    # initial experience data collection
    scale = maxU - minU
    bias = minU
    rnd = lambda x, t: (scale * np.random.rand(U, ) + bias)  # noqa: E731
    while exp.n_samples() < initial_experience:
        ret = utils.apply_controller(
            env, rnd, min(control_H, initial_experience - exp.n_samples() + 1))
        params_ = [p.clone() for p in list(pol.parameters())]
        exp.append_episode(*ret, policy_params=params_)
        exp.save(results_filename)

    # policy learning loop
    for ps_it in range(N_ps):
        # apply policy
        new_exp = exp.n_samples() + control_H
        while exp.n_samples() < new_exp:
            ret = utils.apply_controller(
                env,
                pol,
                min(control_H, new_exp - exp.n_samples() + 1),
                callback=lambda *args, **kwargs: env.render())
            params_ = [p.clone() for p in list(pol.parameters())]
            exp.append_episode(*ret, policy_params=params_)
            exp.save(results_filename)

        # train dynamics
        X, Y = exp.get_dynmodel_dataset(deltas=True, return_costs=learn_reward)
        dyn.set_dataset(X.to(dyn.X.device).float(), Y.to(dyn.X.device).float())
        utils.train_regressor(dyn,
                              N_dynopt,
                              dyn_batch_size,
                              True,
                              opt1,
                              log_likelihood=dyn.output_density.log_prob,
                              summary_writer=writer,
                              summary_scope='model_learning/episode_%d' %
                              ps_it)

        # sample initial states for policy optimization
        x0 = exp.sample_states(pol_batch_size,
                               timestep=0).to(dyn.X.device).float()
        x0 = x0 + 1e-2 * x0.std(0) * torch.randn_like(x0)
        x0 = x0.detach()

        utils.plot_rollout(x0[:25], dyn, pol, pred_H * 2)

        # train policy
        def on_iteration(i, loss, states, actions, rewards, discount):
            writer.add_scalar('mc_pilco/episode_%d/training loss' % ps_it,
                              loss, i)
            if i % 100 == 0:
                writer.flush()

        print("Policy search iteration %d" % (ps_it + 1))
        algorithms.mc_pilco(x0,
                            dyn,
                            pol,
                            max(10, min(pred_H, ps_it)),
                            opt2,
                            exp,
                            N_polopt,
                            pegasus=True,
                            mm_states=False,
                            mm_rewards=False,
                            maximize=True,
                            clip_grad=1.0,
                            step_idx_to_sample=None,
                            on_iteration=on_iteration)
        utils.plot_rollout(x0[:25], dyn, pol, pred_H * 2)
        writer.add_scalar('robot/evaluation_loss',
                          torch.tensor(ret[2]).sum(), ps_it + 1)
