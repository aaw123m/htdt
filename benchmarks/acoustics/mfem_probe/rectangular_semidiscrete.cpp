#include "mfem.hpp"

#include <chrono>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {

void WriteSparse(std::ofstream &os, const char *name, mfem::SparseMatrix &m)
{
   const int rows = m.Height(), cols = m.Width(), nnz = m.NumNonZeroElems();
   os << "  \"" << name << "\": {\n";
   os << "    \"rows\": " << rows << ", \"cols\": " << cols << ", \"nnz\": " << nnz << ",\n";
   os << "    \"row_offsets\": [";
   for (int i = 0; i <= rows; ++i) { if (i) os << ", "; os << m.GetI()[i]; }
   os << "],\n    \"column_indices\": [";
   for (int i = 0; i < nnz; ++i) { if (i) os << ", "; os << m.GetJ()[i]; }
   os << "],\n    \"values\": [";
   for (int i = 0; i < nnz; ++i) { if (i) os << ", "; os << m.GetData()[i]; }
   os << "]\n  }";
}

void WriteVector(std::ofstream &os, const char *name, const mfem::Vector &v)
{
   os << "  \"" << name << "\": [";
   for (int i = 0; i < v.Size(); ++i) { if (i) os << ", "; os << v[i]; }
   os << "]";
}

mfem::Mesh BuildBox(double ox, double oy, double oz, double lx, double ly, double lz)
{
   mfem::Mesh mesh(3, 8, 1, 0, 3);
   const double xyz[8][3] = {
      {ox, oy, oz}, {ox + lx, oy, oz}, {ox + lx, oy + ly, oz}, {ox, oy + ly, oz},
      {ox, oy, oz + lz}, {ox + lx, oy, oz + lz},
      {ox + lx, oy + ly, oz + lz}, {ox, oy + ly, oz + lz}
   };
   for (const auto &p : xyz) { mesh.AddVertex(p); }
   mesh.AddHex(0, 1, 2, 3, 4, 5, 6, 7, 1);
   mesh.FinalizeHexMesh(1, 0, true);
   return mesh;
}

int ProbeMain(int argc, char *argv[])
{
   double ox = 0.0, oy = 0.0, oz = 0.0;
   double lx = 6.0, ly = 4.0, lz = 2.5;
   double density = 1.2, c = 343.0;
   double sx = 1.0, sy = 1.0, sz = 1.0;
   double rx = 5.0, ry = 3.0, rz = 1.0;
   int order = 2, refinements = 0;
   std::string output;

   for (int i = 1; i < argc; ++i)
   {
      const std::string arg = argv[i];
      auto next = [&](const char *name) {
         if (i + 1 >= argc) throw std::runtime_error(std::string("missing value for ") + name);
         return std::string(argv[++i]);
      };
      if (arg == "--origin-x") ox = std::stod(next("--origin-x"));
      else if (arg == "--origin-y") oy = std::stod(next("--origin-y"));
      else if (arg == "--origin-z") oz = std::stod(next("--origin-z"));
      else if (arg == "--lx") lx = std::stod(next("--lx"));
      else if (arg == "--ly") ly = std::stod(next("--ly"));
      else if (arg == "--lz") lz = std::stod(next("--lz"));
      else if (arg == "--density") density = std::stod(next("--density"));
      else if (arg == "--sound-speed") c = std::stod(next("--sound-speed"));
      else if (arg == "--source-x") sx = std::stod(next("--source-x"));
      else if (arg == "--source-y") sy = std::stod(next("--source-y"));
      else if (arg == "--source-z") sz = std::stod(next("--source-z"));
      else if (arg == "--receiver-x") rx = std::stod(next("--receiver-x"));
      else if (arg == "--receiver-y") ry = std::stod(next("--receiver-y"));
      else if (arg == "--receiver-z") rz = std::stod(next("--receiver-z"));
      else if (arg == "--order") order = std::stoi(next("--order"));
      else if (arg == "--uniform-refinements") refinements = std::stoi(next("--uniform-refinements"));
      else if (arg == "--output") output = next("--output");
      else throw std::runtime_error("unknown argument: " + arg);
   }

   if (output.empty()) throw std::runtime_error("--output is required");
   if (!(lx > 0 && ly > 0 && lz > 0 && density > 0 && c > 0)) throw std::runtime_error("physical box/environment values must be positive");
   if (order != 2 || refinements < 0 || refinements > 2) throw std::runtime_error("this frozen probe permits only H1 p2 / h-refinements 0..2");

   mfem::Mesh mesh = BuildBox(ox, oy, oz, lx, ly, lz);
   for (int i = 0; i < refinements; ++i) mesh.UniformRefinement();

   mfem::H1_FECollection fec(order, 3);
   mfem::FiniteElementSpace fes(&mesh, &fec);
   const auto started = std::chrono::steady_clock::now();

   mfem::BilinearForm mass(&fes);
   mass.AddDomainIntegrator(new mfem::MassIntegrator);
   mass.Assemble();
   mass.Finalize();

   mfem::ConstantCoefficient c2(c * c);
   mfem::BilinearForm stiffness(&fes);
   stiffness.AddDomainIntegrator(new mfem::DiffusionIntegrator(c2));
   stiffness.Assemble();
   stiffness.Finalize();

   mfem::DeltaCoefficient source_delta(sx, sy, sz, 1.0);
   mfem::LinearForm source(&fes);
   source.AddDomainIntegrator(new mfem::DomainLFIntegrator(source_delta));
   source.Assemble();

   mfem::DeltaCoefficient receiver_delta(rx, ry, rz, 1.0);
   mfem::LinearForm receiver(&fes);
   receiver.AddDomainIntegrator(new mfem::DomainLFIntegrator(receiver_delta));
   receiver.Assemble();

   const double assembly_s = std::chrono::duration<double>(
      std::chrono::steady_clock::now() - started).count();
   mfem::SparseMatrix &M = mass.SpMat();
   mfem::SparseMatrix &K = stiffness.SpMat();

   std::ofstream os(output, std::ios::binary);
   if (!os) throw std::runtime_error("cannot open output: " + output);
   os << std::setprecision(17);
   os << "{\n";
   os << "  \"schema_version\": \"r100b-mfem-rectangular-semidiscrete-system-1\",\n";
   os << "  \"mfem_version\": \"" << MFEM_VERSION_STRING << "\",\n";
   os << "  \"fixture_id\": \"wave-rectangular-convergence-v1\",\n";
   os << "  \"geometry\": \"exact-axis-aligned-r100a-box\",\n";
   os << "  \"boundary_model\": \"natural-neumann-rigid\",\n";
   os << "  \"primary_field\": \"velocity_potential_phi\",\n";
   os << "  \"governing_equation\": \"M*phi_tt+Kc2*phi=c^2*b*q\",\n";
   os << "  \"mass_assembly\": \"MFEM MassIntegrator\",\n";
   os << "  \"stiffness_assembly\": \"MFEM DiffusionIntegrator(c^2)\",\n";
   os << "  \"source_functional_assembly\": \"MFEM DomainLFIntegrator(DeltaCoefficient)\",\n";
   os << "  \"receiver_functional_assembly\": \"MFEM DomainLFIntegrator(DeltaCoefficient)\",\n";
   os << "  \"matrix_format\": \"csr_full\",\n";
   os << "  \"order\": " << order << ",\n";
   os << "  \"uniform_refinements\": " << refinements << ",\n";
   os << "  \"elements\": " << mesh.GetNE() << ",\n";
   os << "  \"ndofs\": " << fes.GetTrueVSize() << ",\n";
   os << "  \"origin_m\": [" << ox << ", " << oy << ", " << oz << "],\n";
   os << "  \"dimensions_m\": [" << lx << ", " << ly << ", " << lz << "],\n";
   os << "  \"density_kg_m3\": " << density << ",\n";
   os << "  \"sound_speed_m_s\": " << c << ",\n";
   os << "  \"source_position_m\": [" << sx << ", " << sy << ", " << sz << "],\n";
   os << "  \"receiver_position_m\": [" << rx << ", " << ry << ", " << rz << "],\n";
   os << "  \"source_normalization\": \"volume_velocity_m3_s\",\n";
   os << "  \"assembly_s\": " << assembly_s << ",\n";
   WriteSparse(os, "mass_matrix", M); os << ",\n";
   WriteSparse(os, "stiffness_c2_matrix", K); os << ",\n";
   WriteVector(os, "source_functional", source); os << ",\n";
   WriteVector(os, "receiver_functional", receiver); os << "\n}\n";
   return 0;
}
}

int main(int argc, char *argv[])
{
   try { return ProbeMain(argc, argv); }
   catch (const std::exception &e) { std::cerr << e.what() << std::endl; return 2; }
}
