package toolcall

import (
	"encoding/json"
	"testing"
)

func TestPathArgProjectionHonorsClientSchema(t *testing.T) {
	tests := []struct {
		name        string
		tools       []any
		toolName    string
		input       string
		wantKey     string
		unwantedKey string
	}{
		{
			name: "Zoo read_file uses path",
			tools: []any{map[string]any{
				"type": "function",
				"function": map[string]any{
					"name": "read_file",
					"parameters": map[string]any{
						"type":       "object",
						"properties": map[string]any{"path": map[string]any{"type": "string"}},
						"required":   []any{"path"},
					},
				},
			}},
			toolName:    "read_file",
			input:       `{"file_path":"notes.md","mode":"slice"}`,
			wantKey:     "path",
			unwantedKey: "file_path",
		},
		{
			name: "Claude Edit keeps file_path",
			tools: []any{map[string]any{
				"name": "Edit",
				"input_schema": map[string]any{
					"type":       "object",
					"properties": map[string]any{"file_path": map[string]any{"type": "string"}},
					"required":   []any{"file_path"},
				},
			}},
			toolName:    "Edit",
			input:       `{"file_path":"notes.md","old_string":"a","new_string":"b"}`,
			wantKey:     "file_path",
			unwantedKey: "path",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			keys := PathArgKeyMap(tt.tools)
			got := ProjectPathArgsForClient(tt.input, keys[tt.toolName])
			var args map[string]any
			if err := json.Unmarshal([]byte(got), &args); err != nil {
				t.Fatalf("invalid projected JSON %q: %v", got, err)
			}
			if args[tt.wantKey] != "notes.md" {
				t.Fatalf("%s=%v, want notes.md; args=%s", tt.wantKey, args[tt.wantKey], got)
			}
			if _, exists := args[tt.unwantedKey]; exists {
				t.Fatalf("unexpected %s in projected args=%s", tt.unwantedKey, got)
			}
		})
	}
}
