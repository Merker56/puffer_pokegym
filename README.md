# Pokegym

Pokemon Red Gymnasium environment for reinforcement learning

## Mac/Linux Installation

1. Clone the repo to your local machine and install it.
2. Fork the repo and clone your fork to your local machine.

```sh
pip install -e . 
```

### Running

```sh
./run.sh
```

## Windows Installation
1. You need to enable WSL
 1.a - open command line type 'wsl install' ** You will need to be administrator **
 1.b - restart your computer for installation to finish
2. This will default install Ubuntu 24.04 with the most recent version of Python. As of writing that is 3.12
3. Because Python 3.12 is missing some dependencies, you will need to install Python 3.11
 3.a Make sure all packages are updated: 'sudo apt update -y && sudo apt upgrade -y'
 3.b Add the 'Deadsnakes' repo to allow you to install Python 3.11: 'sudo add-apt-repository ppa:deadsnakes/ppa'
 3.c Now install Python 3.11: 'sudo apt-get install python3.11'
 3.d Now install venv for Python3.11: 'sudo apt install python3.11-venv'
 3.e Now install dev for Python3.11: 'sudo apt install python3.11-dev'
4. Clone your fork of the repository: git clone 'https://github.com/<<GITHUB USERNAME>>/puffer_pokegym.git'
5. Create your virtual environment. The key here is specifying 3.11 on the python version. Change the part after venv to whatever you would like to name your environment: 'python3.11 -m venv pokeyred_puffer'
6. Install the packages the environment needs with: 'pip install -e .'
7. In your code directory, open pokemon_red.ini and make adjustments to num_envs, num_workers, stream_wrapper_name
8. In demo.py, update the wandb settings (around lines 133 and 134) if you are using your own directory. wandb-entity is your Team/Group in the WandB site, wandb-project is your project name.
9. Make sure you copy your 'pokemon_red.gb' file to the main directory of where you made the git clone. 
### Structure

/wrappers: Contains environment wrappers for customizing and extending the base environment.

/pokegym: Holds the core environment files. Modify these files to alter the environment's behavior.

/policies: Includes policy implementations. This is where you'll define and test different reinforcement learning strategies.

/config: Configuration files for setting parameters and environment settings.

## Powered by Pufferlib
