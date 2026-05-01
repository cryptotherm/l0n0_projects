module.exports = {
  apps: [{
    name: 'miner-testbench',
    script: '/usr/bin/python3',
    args: '-m uvicorn main:app --host 0.0.0.0 --port 8000',
    cwd: '/home/ctadmin/miner-testbench/server',
    env: {
      DATABASE_URL: 'postgresql+asyncpg://testbench:testbench@localhost:5460/testbench',
      OPENAI_API_KEY: process.env.OPENAI_API_KEY,
      PRINTER_MODEL: 'QL-810W',
      PRINTER_IDENTIFIER: 'tcp://10.0.0.166',
      LABEL_TYPE: '62',
      MINER_USERNAME: 'root',
      MINER_PASSWORD: 'root',
      PORT: '8000',
      HOST: '0.0.0.0',
      LOG_LEVEL: 'info'
    }
  }]
};
