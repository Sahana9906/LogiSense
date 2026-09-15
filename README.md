python -m logisense.cli detect-incidents
set PYTHONPATH=src && python -m logisense.cli start-investigation --incident-id 5
set PYTHONPATH=src && python -m logisense.cli generate-hypotheses --run-id <run_id>
set PYTHONPATH=src && python -m logisense.cli validate-hypotheses --run-id <run_id> --include-external
set PYTHONPATH=src && python -m logisense.cli analyze "What evidence in the company records explains the 4-day delivery delay of Shipment ID 10 to Miami?"
