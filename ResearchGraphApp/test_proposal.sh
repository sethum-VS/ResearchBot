#!/bin/bash

# Move to the ResearchGraphApp directory
cd "$(dirname "$0")"

echo "Testing generate_proposal with a demo project idea..."
echo ""

PROJECT_IDEA=$(cat << 'EOF'
High-Degree Limitations
• Architectural Limitations of Vector Databases in Multi-Agent Systems
  Vector Databases, a cornerstone of many RAG-based memory systems, are identified across multiple sources as having fundamental weaknesses when applied to complex multi-agent workflows. Their flat, centralized architecture is ill-suited for temporal reasoning, making auditability difficult and inefficient. Furthermore, a centralized vector DB creates a performance bottleneck and a significant security and privacy risk by aggregating sensitive data from all participating agents.
  Evidence: One source paper explicitly details the "Methodological Weaknesses" of vector databases, highlighting "Poor Temporal and Relational Auditability" and "Centralization as a Bottleneck and Security Risk". Another source critiques the broader RAG paradigm, which relies heavily on vector databases, for lacking essential infrastructure for governance, schema enforcement, and session-aware context delivery in a multi-agent context.
Orphaned Solutions
• Vector Database
  The source literature criticizes centralized vector databases as a primary memory architecture for multi-agent systems, stating they are 'fundamentally ill-suited for temporal reasoning'. This makes it difficult to debug agent decision histories or audit compliance over time. Furthermore, their centralized nature introduces performance bottlenecks and significant security risks by aggregating sensitive data from multiple agents into a single point of failure.
  Contribution: Develop a decentralized, agent-centric memory module that encapsulates temporal history, analogous to the 'Temporal Memory Capsules' concept. This FYP would involve creating a local, time-stamped log for each agent and building an associated 'time-travel' dashboard to visualize and query an agent's state transitions, directly addressing the auditability limitations of centralized vector stores.

• RAG
  According to the 'Governed Memory' paper, Retrieval-Augmented Generation (RAG) is insufficient as an infrastructure layer for enterprise multi-agent systems. The paper argues that RAG is a 'retrieval primitive, not an infrastructure layer' that lacks mechanisms for governing memory writes, routing organizational policies, managing context efficiently across multi-step autonomous tasks, or monitoring the quality of stored information, creating a 'memory governance gap'.
  Contribution: Implement a 'progressive context delivery' module for a RAG-based agent system. The module will maintain a session state to track information already delivered to the agent's context window during a multi-step task. It will then inject only new or newly relevant context at each subsequent step, aiming to reduce token consumption and improve model attention, addressing the context redundancy limitation of standard RAG.
EOF
)

# Execute the pipeline script the same way the Swift frontend does
./execute_pipeline.sh \
  --command generate_proposal \
  --session-id "session_20260525T004344Z_ai_agents_episodic_vs_semantic_memory_shift_the_focus_from_s" \
  --project-idea "$PROJECT_IDEA"
