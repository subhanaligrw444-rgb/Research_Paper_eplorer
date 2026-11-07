# Research Paper Explorer

**An Interactive Research Paper Discovery and Algorithm Benchmarking Platform**

Research Paper Explorer is a Python-based application designed to help users discover, search, organize, and explore academic research papers. It integrates with the **Crossref API** to retrieve scholarly publication metadata and provides an interactive dashboard with searching, sorting, and algorithm performance benchmarking capabilities.

The project combines real-world API integration, data processing, data structures and algorithms, and web-based visualization in one application.

---

## Project Overview

Finding relevant academic research papers can be challenging when dealing with large collections of publications.

Research Paper Explorer provides an interactive environment where users can explore a dataset of 15,000 scholarly publications, perform searches, organize results, and compare the performance of different sorting algorithms.

The application also demonstrates how different algorithms perform when working with real-world academic publication data.

## Key Features

### 1. Research Paper Data Collection
- Integration with the Crossref API.
- Retrieval of academic publication metadata.
- Dataset containing 15,000 research paper records.
- Storage and processing of publication information.
- Support for data collection and dataset management.

### 2. Interactive Dashboard
- Web-based user interface.
- Organized research publication display.
- Dashboard for exploring academic publications.
- Multiple sections for searching, sorting, and benchmarking.

### 3. Advanced Search
- Search through research paper records.
- Explore publication information.
- Filter and organize research results using the available search functionality.

### 4. Sorting Laboratory

Implementation of 10 sorting algorithms:

| Algorithm | Average Time Complexity |
|---|---|
| Bubble Sort | O(n²) |
| Selection Sort | O(n²) |
| Insertion Sort | O(n²) |
| Merge Sort | O(n log n) |
| Quick Sort | O(n log n) |
| Radix Sort | Depends on digits and base |
| Bucket Sort | Depends on input distribution |
| Heap Sort | O(n log n) |
| Shell Sort | Depends on gap sequence |
| Hybrid Sort | Depends on implementation |

### 5. Algorithm Benchmarking
- Compare sorting algorithm performance.
- Measure execution time.
- Analyze comparisons, swaps, and data movements where supported.
- Explore the impact of dataset size on algorithm performance.
- Understand practical differences between sorting algorithms.

### 6. Data Management
- Work with research publication datasets.
- Manage the collected publication records.
- Monitor data collection status through the application.

---

## Technologies Used

- **Python** – Backend development and algorithm implementation.
- **HTML5** – Web page structure.
- **CSS3** – User interface styling.
- **JavaScript** – Frontend interactions, where implemented.
- **Crossref REST API** – Academic publication metadata retrieval.
- **CSV** – Dataset storage and management.
- **Git & GitHub** – Version control and project hosting.

---

## Dataset Information

The project includes a dataset of:

- **15,000** research paper records.
- **11** data columns.
- Academic publication metadata retrieved through Crossref.

The dataset provides the records used by the application for searching, sorting, and algorithm benchmarking.

---

## Project Structure

```text
Research-Paper-Explorer/
│
├── app.py
├── index.html
├── crossref_dataset.csv
├── README.md
│
└── Additional project files
```

*The structure above highlights the main project files. Other supporting files may also be included.*

---

## Installation and Setup

### 1. Clone the Repository

```bash
git clone https://github.com/subhanaligrw444-rgb/Research_Paper_eplorer.git
```

### 2. Navigate to the Project Directory

```bash
cd Research_Paper_eplorer
```

### 3. Create a Virtual Environment (Recommended)

```bash
python -m venv .venv
```

Activate it on Windows:

```powershell
.venv\Scripts\activate
```

### 4. Install Dependencies

If a `requirements.txt` file is included:

```bash
pip install -r requirements.txt
```

Otherwise, install the dependencies specified by the project.

### 5. Run the Application

```bash
python app.py
```

### 6. Open the Application

Open the local address displayed in your terminal, typically:

```text
http://127.0.0.1:8000
```

---

## API Endpoints

The application exposes backend endpoints including:

| Endpoint | Purpose |
|---|---|
| `/api/scraper/status` | Check data collection status |
| `/api/sort` | Access sorting functionality |

---

## Learning Outcomes

This project demonstrates practical implementation of:

- Data Structures and Algorithms.
- Time Complexity and Space Complexity.
- API integration and data retrieval.
- Dataset processing and management.
- Searching and sorting techniques.
- Algorithm benchmarking and performance analysis.
- Frontend and backend integration.
- Real-world software development practices.

---

## Future Improvements

Potential improvements include:

- More advanced research paper filtering.
- Additional sorting and searching algorithms.
- Enhanced performance visualization.
- Exporting filtered research results.
- Improved dataset management.
- More detailed algorithm comparison reports.

---

## Project Information

**Project Name:** Research Paper Explorer

**Category:** Software Engineering / Data Structures and Algorithms

**Primary Language:** Python

**Data Source:** Crossref API

**Dataset Size:** 15,000 Research Papers

**Repository:** [Research Paper Explorer on GitHub](https://github.com/subhanaligrw444-rgb/Research_Paper_eplorer)

---

## Author

**Subhan Ali**

Computer Science Student | Software Developer

GitHub: [@subhanaligrw444-rgb](https://github.com/subhanaligrw444-rgb)

---

*Built as an academic software development project to explore scholarly publications and demonstrate practical algorithm implementation and benchmarking.*
