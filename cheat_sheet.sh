# One-time
conda create -n ibkrbot python=3.10 -y
conda activate ibkrbot
conda install pip -y
pip install ib_insync

# Every run
conda activate ibkrbot
cd /Users/rajesh/Documents/Trade\ Automation/code/
caffeinate -i python ibkr_csp_bot.py

# Normal
python ibkr_csp_bot.py

#Dry Run
python ibkr_csp_bot.py --dry-run

#Dry Run & loop forever with 60 sec interval
python ibkr_csp_bot.py --dry-run --loop --interval 60

#Dry Run & test mode & loop forever with 60 sec interval
python ibkr_csp_bot.py --test-mode --dry-run --loop --interval 60

# Connect to Paper or Live TWS/Gateway
python ibkr_csp_bot.py --port 7497   # paper
python ibkr_csp_bot.py --port 7496   # live

pip install ib_insync pandas lxml