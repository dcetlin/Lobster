# Model Selection for Claude Prescription

## Overview

The model selection feature allows flexible configuration of which Claude model is used for WOS prescription dispatch. This enables budget optimization by using lower-cost models (e.g., Haiku) when available, while maintaining the ability to use more capable models (e.g., Opus) when needed.

## Configuration

Model selection follows this priority order:

1. **Environment Variable** (highest priority): `LOBSTER_PRESCRIPTION_MODEL`
2. **wos-config.json**: `prescription_model` field
3. **Default**: `opus` (lowest priority)

### Environment Variable

Set `LOBSTER_PRESCRIPTION_MODEL` to override model selection:

```bash
export LOBSTER_PRESCRIPTION_MODEL=haiku
# Now all prescriptions will use Haiku model
```

This takes precedence over any configuration file settings.

### Configuration File (wos-config.json)

Set the `prescription_model` field in `~/lobster-workspace/data/wos-config.json`:

```json
{
  "execution_enabled": true,
  "prescription_model": "haiku"
}
```

Available models:
- `opus` - Default, most capable
- `sonnet-4` - Balanced capability and cost
- `haiku` - Most cost-effective
- Other Claude models as they become available

### Default Behavior

If neither environment variable nor config file is set, the system defaults to `opus`.

## Implementation Details

### Dispatch Flow

When the Steward prescribes a new Unit of Work:

1. `select_steward_model(uow)` selects the model tier based on UoW type and cycle count
2. The `claude -p` subprocess is invoked with `--model <MODEL>` flag
3. Prescription execution logs include the model used (INFO level)

### Logging

Successful prescriptions log the model used:

```
_llm_prescribe: LLM prescription generated for <uow_id> (model=haiku, estimated_cycles=1)
```

This allows audit trails to track which models handled which prescriptions.

### Error Handling

If prescription dispatch fails:
- The Steward falls back to deterministic prescription (no model dependency)
- Model selection does not affect fallback logic
- Fallback paths are identical regardless of model choice

## Examples

### Use Haiku for Cost Optimization

```bash
# In your shell or systemd service environment
export LOBSTER_PRESCRIPTION_MODEL=haiku
```

Then restart the Steward:
```bash
~/lobster/scripts/restart-mcp.sh
```

### Use Opus for Complex Tasks

```bash
# Update the config file
cat > ~/lobster-workspace/data/wos-config.json <<'EOF'
{
  "execution_enabled": true,
  "prescription_model": "opus"
}
EOF
```

The next Steward invocation will use this model.

### Runtime Change (No Restart)

Since `wos-config.json` is read on every Steward cycle, you can change the model without restarting:

```bash
# Update config
cat > ~/lobster-workspace/data/wos-config.json <<'EOF'
{
  "execution_enabled": true,
  "prescription_model": "sonnet-4"
}
EOF

# Next Steward cycle (within ~3 minutes) will use the new model
```

## Testing

Test the feature with:

```bash
# Test env var priority
export LOBSTER_PRESCRIPTION_MODEL=haiku
uv run python /home/lobster/test_model_selection.py

# Test config integration
uv run python /home/lobster/test_model_config.py
```

## Migration Notes

Existing installations without this feature will use the default (`opus`). No action required unless you want to use a different model.

If you have `wos-config.json` without a `prescription_model` field, the field will be automatically added on the next config write (e.g., when using `/wos start` command). Manual configuration is optional.

## Troubleshooting

### Prescription Using Wrong Model

Check priority order:
1. `echo $LOBSTER_PRESCRIPTION_MODEL` - Is env var set?
2. `cat ~/lobster-workspace/data/wos-config.json` - Does config have `prescription_model`?
3. Check logs: `grep "model=" ~/lobster-workspace/logs/steward.log`

### Model Not Found Error

If you specify an invalid or unavailable model:
- Fallback to deterministic prescription is triggered
- Check `/home/lobster/lobster-workspace/logs/steward.log` for error details
- Verify model name matches Claude API availability
