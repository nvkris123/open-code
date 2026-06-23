# Sample note

A nested file to demonstrate the folder tree.

## Architecture sketch

```mermaid
flowchart LR
    Client -- HTTP --> Express
    Express -- read --> FS[(content/)]
    Express -- HTML --> Browser
    Browser -- JS --> Mermaid
    Browser -- JS --> Drawio
```
