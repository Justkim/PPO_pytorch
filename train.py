from model import Model
import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
import flag
import datetime
import ray
import mario_env
from baselines import logger
import time
from torch.distributions.categorical import Categorical
from rnd_model import TargetModel,PredictorModel
from utils import RunningStdMean,RewardForwardFilter

@ray.remote
class Simulator(object):
    def __init__(self,num_action_repeat):
        self.env = mario_env.make_train_0()
        self.env.reset()
        self.num_action_repeat=num_action_repeat

    def step(self, action):
        for i in range(self.num_action_repeat):
            observations,rewards,dones,info=self.env.step(action)
            if dones:
                observations = self.reset()
        if flag.SHOW_GAME:
            self.env.render()
        return observations, rewards, dones

    def reset(self):
        return self.env.reset()


class Trainer():
    def __init__(self,num_training_steps,num_env,num_game_steps,num_epoch,
                 learning_rate,discount_factor,num_action,
                 value_coef,clip_range,save_interval,log_interval,entropy_coef,lam,mini_batch_size,num_action_repeat,load_path,ext_adv_coef,int_adv_coef,num_pre_norm_steps):
        self.training_steps=num_training_steps
        self.num_epoch=num_epoch
        self.learning_rate=learning_rate
        self.discount_factor=discount_factor
        self.num_game_steps=num_game_steps
        self.num_env=num_env
        self.batch_size=num_env*num_game_steps
        self.clip_range=clip_range
        self.value_coef=value_coef
        self.entropy_coef = entropy_coef
        self.mini_batch_size=mini_batch_size
        self.num_action=num_action
        self.num_pre_norm_steps=num_pre_norm_steps

        assert self.batch_size % self.mini_batch_size == 0
        self.mini_batch_num=int(self.batch_size / self.mini_batch_size)
        self.current_time = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        log_dir = 'logs/' + self.current_time + '/log'
        logger.configure(dir=log_dir)
        self.save_interval=save_interval
        self.lam=lam
        self.log_interval=log_interval

        self.num_action_repeat=num_action_repeat
        self.clip_range = clip_range

        self.value_coef = value_coef
        self.entropy_coef = entropy_coef
        self.load_path=load_path

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.new_model = Model(self.num_action).to(self.device)
        self.optimizer = optim.Adam(self.new_model.parameters(), lr=self.learning_rate)
        self.ext_adv_coef=ext_adv_coef
        self.int_adv_coef=int_adv_coef

        logger.record_tabular("time: ", self.current_time)
        logger.record_tabular("num_env: ", self.num_env)
        logger.record_tabular("steps: ", self.num_game_steps)
        logger.record_tabular("mini batch: ", self.mini_batch_size)
        logger.record_tabular("lr: ", self.learning_rate)
        logger.record_tabular("gamma: ", self.discount_factor)
        logger.record_tabular("lambda: ", self.lam)
        logger.record_tabular("clip: ", self.clip_range)
        logger.record_tabular("v_coef: ", self.value_coef)
        logger.record_tabular("ent_coef: ", self.entropy_coef)
        logger.dump_tabular()
        self.target_model = TargetModel(self.num_action).to(self.device)
        self.predictor_model = PredictorModel(self.num_action).to(self.device)
        self.mse_loss = nn.MSELoss()

        self.reward_rms = RunningStdMean()
        self.obs_rms = RunningStdMean(shape=(1, 1, 84, 84))
        self.reward_filter = RewardForwardFilter(0.99)

    def collect_experiance_and_train(self):
        start_train_step = 0

        if flag.LOAD:
            checkpoint = torch.load(self.load_path)

            self.new_model.load_state_dict(checkpoint['model_state_dict'])
            self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
            start_train_step= checkpoint['train_step']
            print("loaded model weights from checkpoint")

        ray.init()
        current_observations = []
        runners = []
        returned_observations = []

        for i in range(self.num_env):
            runners.append(Simulator.remote(self.num_action_repeat))
            returned_observations.append(runners[i].reset.remote())
        for i in range(self.num_env):
            current_observations.append(ray.get(returned_observations[i]))

        #normalize observations
        observations_to_normalize=[]
        for step in range(self.num_game_steps* self.num_pre_norm_steps):
            returned_objects = []
            experiences=[]
            actions=np.random.randint(0,self.num_action,size=(self.num_env))
            print(actions)
            for i in range(self.num_env):
                returned_objects.append(runners[i].step.remote(actions[i]))
                experiences.append(ray.get(returned_objects[i]))
            current_observations = [each[0] for each in experiences]
            observations_to_normalize.extend(current_observations)
            if(len(observations_to_normalize)%(self.num_game_steps*self.num_env)==0):
                observations_to_normalize=np.stack(observations_to_normalize)[:,3,:,:].reshape(-1,1,84,84)
                print(observations_to_normalize.shape)
                self.obs_rms.update(observations_to_normalize)
                observations_to_normalize=[]

        current_observations = []
        returned_observations=[]

        for i in range(self.num_env):
            runners.append(Simulator.remote(self.num_action_repeat))
            returned_observations.append(runners[i].reset.remote())
        for i in range(self.num_env):
            current_observations.append(ray.get(returned_observations[i]))





        for train_step in range(start_train_step,self.training_steps):

            observations=[]
            int_rewards=[]
            ext_rewards=[]
            dones=[]
            int_values=[]
            ext_values=[]
            actions=[]

            start=time.time()
            cross_entropy_loss = nn.CrossEntropyLoss()

            for game_step in range(self.num_game_steps):
                returned_objects = []
                # observations.extend(current_observations)
                observations.extend(current_observations)
                print("very imp",np.array(current_observations).shape)
                with torch.no_grad():
                    current_observations_tensor = torch.from_numpy(np.array(current_observations)).float().to(self.device)
                    decided_actions, predicted_ext_values, predicted_int_values = self.new_model.step(current_observations_tensor)
                    # print(np.array(current_observations).shape)
                    # print("lalaaa",np.array(current_observations)[:,3,:,:].reshape(-1,1,84,84))
                    one_channel_observations=np.array(current_observations)[:,3,:,:].reshape(-1,1,84,84)
                    # print(one_channel_observations.shape)

                    one_channel_observations_tensor=torch.from_numpy(one_channel_observations).float().to(self.device)
                    print("lalalalala",one_channel_observations_tensor.shape)
                    int_rewards.append(self.get_intrinsic_rewards(one_channel_observations_tensor))

                int_values.append(predicted_int_values)
                ext_values.append(predicted_ext_values)
                actions.extend(decided_actions)

                experiences=[]
                for i in range(self.num_env):
                        returned_objects.append(runners[i].step.remote(decided_actions[i]))
                        experiences.append(ray.get(returned_objects[i]))
                current_observations=[each[0] for each in experiences]
                ext_rewards.append([each[1] for each in experiences])
                dones.append([each[2] for each in experiences])


            # next state value, required for computing advantages
            with torch.no_grad():
                current_observations_tensor = torch.from_numpy(np.array(current_observations)).float().to(self.device)
                decided_actions, predicted_ext_values,predicted_int_values = self.new_model.step(current_observations_tensor)

            int_values.append(predicted_int_values)
            ext_values.append(predicted_ext_values)


            # convert lists to numpy arrays
            observations_array=np.array(observations)
            one_channel_observations=observations_array[:,3,:,:].reshape(-1,1,84,84)
            print("one channel observation shape",one_channel_observations.shape)
            one_channel_observations = ((one_channel_observations - self.obs_rms.mean) / np.sqrt(self.obs_rms.var)).clip(-5,5)
            ext_rewards_array = np.array(ext_rewards)
            int_rewards_array = np.array(int_rewards)
            dones_array = np.array(dones)
            ext_values_array=np.array(ext_values)
            int_values_array = np.array(int_values)
            actions_array = np.array(actions)
            print(np.array(int_rewards).shape)
            # Step 2. calculate intrinsic reward
            # running mean intrinsic reward
            int_reward = np.stack(int_rewards).transpose()
            total_reward_per_env = np.array([self.reward_filter.update(reward_per_step) for reward_per_step in
                                             int_reward.T])
            mean, std, count = np.mean(total_reward_per_env), np.std(total_reward_per_env), len(total_reward_per_env)
            self.reward_rms.update_from_mean_std(mean, std ** 2, count)

            # normalize intrinsic reward
            int_reward /= np.sqrt(self.reward_rms.var)
            print(ext_rewards_array.shape)
            print(ext_values_array.shape)

            ext_advantages_array,ext_returns_array=self.compute_advantage(ext_rewards_array,ext_values_array,dones_array,0)
            int_advantages_array, int_returns_array = self.compute_advantage(int_rewards_array, int_values_array,
                                                                             dones_array,1)

            advantages_array = self.ext_adv_coef * ext_advantages_array + self.int_adv_coef * int_advantages_array
            self.obs_rms.update(one_channel_observations)

            if flag.DEBUG:
                print("all actions are",actions)

            random_indexes=np.arange(self.batch_size)
            np.random.shuffle(random_indexes)
            end=time.time()

            # print("time elapsed in game steps",end-start)
            start=time.time()


            observations_tensor=torch.from_numpy(np.array(observations_array)).float().to(self.device)
            ext_returns_tensor=torch.from_numpy(np.array(ext_returns_array)).float().to(self.device)
            int_returns_tensor = torch.from_numpy(np.array(int_returns_array)).float().to(self.device)
            actions_tensor = torch.from_numpy(np.array(actions_array)).long().to(self.device)
            advantages_tensor=torch.from_numpy(np.array(advantages_array)).float().to(self.device)
            one_channel_observations_tensor=torch.from_numpy(one_channel_observations).float().to(self.device)

            print(observations_tensor.shape)
            print(ext_returns_tensor.shape)
            print(int_returns_tensor.shape)
            print(actions_tensor.shape)
            print(advantages_tensor.shape)
            print(one_channel_observations_tensor.shape)



            with torch.no_grad():
                old_policy, _,_ = self.new_model.forward_pass(observations_tensor)
                old_negative_log_p = cross_entropy_loss(old_policy, actions_tensor)
            loss_avg=[]
            policy_loss_avg=[]
            value_loss_avg=[]
            entropy_avg=[]

            for epoch in range(0,self.num_epoch):
                # print("----------------next epoch----------------")

                for n in range(0,self.mini_batch_num):
                    # print("----------------next mini batch-------------")
                    start_index=n*self.mini_batch_size
                    index_slice=random_indexes[start_index:start_index+self.mini_batch_size]
                    if flag.DEBUG:
                        print("indexed chosen are:",index_slice)

                    experience_slice=(arr[index_slice] for arr in (observations_tensor,ext_returns_tensor,int_returns_tensor,actions_tensor,
                                                                   advantages_tensor,one_channel_observations_tensor))
                    self.obs_rms.update(one_channel_observations)
                    loss, policy_loss, value_loss, entropy=self.train_model(*experience_slice,old_negative_log_p)
                    loss=loss.detach().cpu().numpy()
                    policy_loss = policy_loss.detach().cpu().numpy()
                    value_loss = value_loss.detach().cpu().numpy()
                    entropy = entropy.detach().cpu().numpy()
                    #self.old_model.set_weights(last_weights)
                    loss_avg.append(loss)
                    policy_loss_avg.append(policy_loss)
                    value_loss_avg.append(value_loss)
                    entropy_avg.append(entropy)
            # print("----------------next training step--------------")

            end=time.time()
            # print("epoch time",end-start)
            loss_avg_result=np.array(loss_avg).mean()
            policy_loss_avg_result=np.array(policy_loss_avg).mean()
            value_loss_avg_result=np.array(value_loss_avg).mean()
            entropy_avg_result=np.array(entropy_avg).mean()
            print("training step {:03d}, Epoch {:03d}: Loss: {:.3f}, policy loss: {:.3f}, value loss: {:.3f}, entopy: {:.3f} ".format(train_step,epoch,
                                                                         loss_avg_result,
                                                                        policy_loss_avg_result,
                                                                         value_loss_avg_result,
                                                                         entropy_avg_result))
            # if flag.DEBUG:
            #     print("policy", self.new_model.probs)
            if flag.TENSORBOARD_AVALAIBLE:
                    #add instructions here
                    print("not implemented")
            else:
                if train_step % self.log_interval == 0:
                    logger.record_tabular("train_step", train_step)
                    logger.record_tabular("loss", loss_avg_result)
                    logger.record_tabular("value loss",  value_loss_avg_result)
                    logger.record_tabular("policy loss", policy_loss_avg_result)
                    logger.record_tabular("entropy", entropy_avg_result)
                    logger.record_tabular("rewards avg", np.average(ext_rewards))
                    logger.dump_tabular()


            if train_step % self.save_interval==0:
                train_checkpoint_dir = 'logs/' + self.current_time + "/" + str(train_step)

                torch.save({
                    'train_step': train_step,
                    'model_state_dict': self.new_model.state_dict(),
                    'optimizer_state_dict': self.optimizer.state_dict(),


                }, train_checkpoint_dir)


    def compute_advantage(self, rewards, values, dones, int_flag):
        if flag.DEBUG:
            print("---------computing advantage---------")
            print("rewards are",rewards)
            print("values from steps are",values)

        advantages = []
        last_advantage = 0
        for step in reversed(range(self.num_game_steps)):
            if int_flag:
                is_there_a_next_state = 1
            else:
                 is_there_a_next_state = 1.0 - dones[step]
            delta = rewards[step] + (is_there_a_next_state * self.discount_factor * values[step + 1]) - values[step]
            if flag.USE_GAE:
                    advantage = last_advantage = delta + self.discount_factor * \
                                                 self.lam * is_there_a_next_state * last_advantage
                    advantages.append(advantage)
            else:
                    advantages.append(delta)
        advantages.reverse()

        advantages=np.array(advantages)
        advantages = advantages.flatten()
        values=values[:-1]
        returns=advantages+values.flatten()
        if flag.DEBUG:
            print("all advantages are",advantages)
            print("all returns are",returns)
        return advantages,returns


    def train_model(self,observations_tensor,ext_returns_tensor,int_returns_tensor,actions_tensor,advantages_tensor,one_channel_observations_tensor, old_negative_log_p):

            if flag.USE_STANDARD_ADV:
                advantages_array=advantages_tensor.mean() / (advantages_tensor.std() + 1e-13)
            # print("values from steps",values_array)

            if flag.DEBUG:
                print("input observations shape", observations_tensor.shape)
                print("input rewards shape", ext_returns_tensor.shape)
                print("input actions shape", actions_tensor.shape)
                print("input advantages shape", advantages_tensor.shape)

                print("returns",ext_returns_tensor)
                print("advantages",advantages_tensor)
                print("actions",actions_tensor)


            loss,policy_loss,value_loss,entropy=self.do_train(observations_tensor,ext_returns_tensor,int_returns_tensor,actions_tensor, advantages_tensor,one_channel_observations_tensor, old_negative_log_p)
            return loss,policy_loss,value_loss,entropy

    def do_train(self,observations,ext_returns,int_returns,actions, advantages, one_channel_observations, old_negative_log_p):
        cross_entropy_loss = nn.CrossEntropyLoss()


        self.new_model.train()
        self.predictor_model.train()
        target_value = self.target_model.forward_pass(one_channel_observations)
        predictor_value = self.predictor_model.forward_pass(one_channel_observations)
        predictor_loss = self.mse_loss(predictor_value, target_value.detach())


        new_policy, ext_new_values, int_new_values = self.new_model.forward_pass(observations)

        ext_value_loss=self.mse_loss(ext_new_values,ext_returns)
        int_value_loss = self.mse_loss(int_new_values, ext_returns)
        value_loss = ext_value_loss+int_value_loss
        new_negative_log_p = cross_entropy_loss(new_policy,actions)
        ratio= torch.exp(old_negative_log_p - new_negative_log_p)

        clipped_policy_loss=torch.clamp(ratio,1.0-self.clip_range, 1+self.clip_range)*advantages
        policy_loss=ratio*advantages

        selected_policy_loss=-torch.min(clipped_policy_loss,policy_loss).mean()
        dist = Categorical(logits=new_policy)
        entropy=dist.entropy().mean()
        loss = selected_policy_loss + self.value_coef*value_loss - self.entropy_coef * entropy + predictor_loss
        self.optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm(self.new_model.parameters(),0.5)
        self.optimizer.step()
        return loss, policy_loss, value_loss, entropy




    def get_intrinsic_rewards(self,input_observation):

        target_value=self.target_model.forward_pass(input_observation)
        predictor_value=self.predictor_model.forward_pass(input_observation)
        intrinsic_reward=(target_value - predictor_value).pow(2).sum(1) / 2
        intrinsic_reward= intrinsic_reward.detach().cpu().numpy()
        print("SHAPE",intrinsic_reward.shape)
        return intrinsic_reward #check this




































