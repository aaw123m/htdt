#include "mfem.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace
{

constexpr double kPi = 3.141592653589793238462643383279502884;
constexpr int kNx = 6;
constexpr int kNy = 4;
constexpr int kNz = 2;
constexpr int kRigidBoundaryAttribute = 1;
constexpr int kRadiationBoundaryAttribute = 2;

struct SampleResult
{
   double frequency_hz = 0.0;
   double pressure_real_pa = 0.0;
   double pressure_imag_pa = 0.0;
   int iterations = 0;
   double relative_residual = 0.0;
   bool converged = false;
};

struct OrderResult
{
   int order = 0;
   int ndofs = 0;
   int elements = 0;
   int boundary_faces = 0;
   int radiation_boundary_faces = 0;
   double assemble_s = 0.0;
   double solve_s = 0.0;
   int max_iterations = 0;
   double max_relative_residual = 0.0;
   std::vector<SampleResult> samples;
};

mfem::Mesh BuildRoom()
{
   mfem::Mesh mesh(
      3,
      (kNx + 1) * (kNy + 1) * (kNz + 1),
      kNx * kNy * kNz,
      0,
      3);

   for (int iz = 0; iz <= kNz; ++iz)
   {
      const double z = 2.5 * static_cast<double>(iz) / static_cast<double>(kNz);
      for (int iy = 0; iy <= kNy; ++iy)
      {
         const double y = 4.0 * static_cast<double>(iy) / static_cast<double>(kNy);
         for (int ix = 0; ix <= kNx; ++ix)
         {
            const double x = 6.0 * static_cast<double>(ix) / static_cast<double>(kNx);
            mesh.AddVertex(x, y, z);
         }
      }
   }

   auto vid = [](int ix, int iy, int iz)
   {
      return iz * (kNy + 1) * (kNx + 1) + iy * (kNx + 1) + ix;
   };

   for (int iz = 0; iz < kNz; ++iz)
   {
      for (int iy = 0; iy < kNy; ++iy)
      {
         for (int ix = 0; ix < kNx; ++ix)
         {
            mesh.AddHex(
               vid(ix,     iy,     iz),
               vid(ix + 1, iy,     iz),
               vid(ix + 1, iy + 1, iz),
               vid(ix,     iy + 1, iz),
               vid(ix,     iy,     iz + 1),
               vid(ix + 1, iy,     iz + 1),
               vid(ix + 1, iy + 1, iz + 1),
               vid(ix,     iy + 1, iz + 1),
               1);
         }
      }
   }

   mesh.FinalizeHexMesh(1, 0, true);

   int radiation_faces = 0;
   for (int be = 0; be < mesh.GetNBE(); ++be)
   {
      mfem::Element *element = mesh.GetBdrElement(be);
      const int *vertices = element->GetVertices();
      bool on_xmax = true;
      for (int local = 0; local < element->GetNVertices(); ++local)
      {
         const double *vertex = mesh.GetVertex(vertices[local]);
         if (std::abs(vertex[0] - 6.0) > 1.0e-12)
         {
            on_xmax = false;
            break;
         }
      }
      element->SetAttribute(
         on_xmax ? kRadiationBoundaryAttribute : kRigidBoundaryAttribute);
      if (on_xmax) { ++radiation_faces; }
   }

   mesh.SetAttributes();
   if (radiation_faces != kNy * kNz)
   {
      throw std::runtime_error(
         "unexpected number of x=6 radiation boundary faces: "
         + std::to_string(radiation_faces));
   }
   if (mesh.bdr_attributes.Max() != kRadiationBoundaryAttribute)
   {
      throw std::runtime_error("radiation boundary attribute was not compiled");
   }
   return mesh;
}

OrderResult SolveOrder(
   int order,
   double density_kg_m3,
   double sound_speed_m_s,
   double source_x,
   double source_y,
   double source_z,
   double receiver_x,
   double receiver_y,
   double receiver_z,
   double source_amplitude_m3_s,
   double frequency_start_hz,
   double frequency_stop_hz,
   double frequency_step_hz)
{
   using clock = std::chrono::steady_clock;

   mfem::Mesh mesh = BuildRoom();
   mfem::H1_FECollection fec(order, 3);
   mfem::FiniteElementSpace fes(&mesh, &fec);

   const auto assemble_started = clock::now();

   mfem::BilinearForm stiffness(&fes);
   stiffness.AddDomainIntegrator(new mfem::DiffusionIntegrator);
   stiffness.Assemble();
   stiffness.Finalize();

   mfem::BilinearForm mass(&fes);
   mass.AddDomainIntegrator(new mfem::MassIntegrator);
   mass.Assemble();
   mass.Finalize();

   mfem::Array<int> radiation_bdr(mesh.bdr_attributes.Max());
   radiation_bdr = 0;
   radiation_bdr[kRadiationBoundaryAttribute - 1] = 1;

   mfem::BilinearForm boundary_mass(&fes);
   boundary_mass.AddBoundaryIntegrator(new mfem::MassIntegrator, radiation_bdr);
   boundary_mass.Assemble();
   boundary_mass.Finalize();

   mfem::DeltaCoefficient source_delta(source_x, source_y, source_z, 1.0);
   mfem::LinearForm source_functional(&fes);
   source_functional.AddDomainIntegrator(new mfem::DomainLFIntegrator(source_delta));
   source_functional.Assemble();

   mfem::DeltaCoefficient receiver_delta(receiver_x, receiver_y, receiver_z, 1.0);
   mfem::LinearForm receiver_functional(&fes);
   receiver_functional.AddDomainIntegrator(new mfem::DomainLFIntegrator(receiver_delta));
   receiver_functional.Assemble();

   const auto assemble_finished = clock::now();

   mfem::SparseMatrix &K = stiffness.SpMat();
   mfem::SparseMatrix &M = mass.SpMat();
   mfem::SparseMatrix &B = boundary_mass.SpMat();

   const int ndofs = fes.GetTrueVSize();
   mfem::Array<int> offsets(3);
   offsets[0] = 0;
   offsets[1] = ndofs;
   offsets[2] = ndofs;
   offsets.PartialSum();

   mfem::BlockVector solution(offsets);
   solution = 0.0;
   bool have_initial_guess = false;

   OrderResult result;
   result.order = order;
   result.ndofs = ndofs;
   result.elements = mesh.GetNE();
   result.boundary_faces = mesh.GetNBE();
   for (int be = 0; be < mesh.GetNBE(); ++be)
   {
      if (mesh.GetBdrElement(be)->GetAttribute() == kRadiationBoundaryAttribute)
      {
         ++result.radiation_boundary_faces;
      }
   }
   result.assemble_s =
      std::chrono::duration<double>(assemble_finished - assemble_started).count();

   const auto solve_started = clock::now();
   const int frequency_count = static_cast<int>(
      std::llround((frequency_stop_hz - frequency_start_hz) / frequency_step_hz)) + 1;

   for (int index = 0; index < frequency_count; ++index)
   {
      const double frequency_hz = frequency_start_hz + index * frequency_step_hz;
      const double omega = 2.0 * kPi * frequency_hz;
      const double k = omega / sound_speed_m_s;

      // Frozen R100A-3 authority:
      //   exp(-i*omega*t)
      //   laplacian(p) + k^2 p = i*omega*rho*Q*delta
      //   dp/dn - i*k*p = 0 on b-interface / x=6 m.
      //
      // Weak form:
      //   (K - k^2 M - i*k B_rad) p = -i*omega*rho*Q*f.
      //
      // For p = p_r + i p_i, solve the real block system
      //   [ A0, +kB ] [p_r] = [0]
      //   [-kB,  A0 ] [p_i]   [-omega*rho*Q*f]
      // with A0 = K-k^2M.
      std::unique_ptr<mfem::SparseMatrix> A0(mfem::Add(1.0, K, -k * k, M));
      std::unique_ptr<mfem::SparseMatrix> P0(mfem::Add(1.0, K, k * k, M));
      std::unique_ptr<mfem::SparseMatrix> P(mfem::Add(1.0, *P0, k, B));

      mfem::BlockOperator block_operator(offsets);
      block_operator.SetDiagonalBlock(0, A0.get());
      block_operator.SetDiagonalBlock(1, A0.get());
      block_operator.SetBlock(0, 1, &B, +k);
      block_operator.SetBlock(1, 0, &B, -k);

      mfem::BlockVector rhs(offsets);
      rhs = 0.0;
      rhs.GetBlock(1) = source_functional;
      rhs.GetBlock(1) *= -omega * density_kg_m3 * source_amplitude_m3_s;

      mfem::DSmoother preconditioner_real(*P);
      mfem::DSmoother preconditioner_imag(*P);
      mfem::BlockDiagonalPreconditioner preconditioner(offsets);
      preconditioner.SetDiagonalBlock(0, &preconditioner_real);
      preconditioner.SetDiagonalBlock(1, &preconditioner_imag);

      mfem::GMRESSolver gmres;
      gmres.iterative_mode = have_initial_guess;
      gmres.SetOperator(block_operator);
      gmres.SetPreconditioner(preconditioner);
      gmres.SetKDim(100);
      gmres.SetMaxIter(6000);
      gmres.SetRelTol(1.0e-10);
      gmres.SetAbsTol(0.0);
      gmres.SetPrintLevel(0);

      if (!have_initial_guess) { solution = 0.0; }
      gmres.Mult(rhs, solution);
      const bool converged = gmres.GetConverged();
      have_initial_guess = converged;

      mfem::BlockVector residual(offsets);
      block_operator.Mult(solution, residual);
      residual -= rhs;
      const double rhs_norm = rhs.Norml2();
      const double relative_residual =
         residual.Norml2() / std::max(rhs_norm, 1.0e-30);

      SampleResult sample;
      sample.frequency_hz = frequency_hz;
      sample.pressure_real_pa = receiver_functional * solution.GetBlock(0);
      sample.pressure_imag_pa = receiver_functional * solution.GetBlock(1);
      sample.iterations = gmres.GetNumIterations();
      sample.relative_residual = relative_residual;
      sample.converged = converged;
      result.samples.push_back(sample);
      result.max_iterations = std::max(result.max_iterations, sample.iterations);
      result.max_relative_residual =
         std::max(result.max_relative_residual, sample.relative_residual);
   }

   const auto solve_finished = clock::now();
   result.solve_s =
      std::chrono::duration<double>(solve_finished - solve_started).count();
   return result;
}

void WriteOrder(std::ofstream &os, const OrderResult &value, bool trailing_comma)
{
   os << "    {\n";
   os << "      \"order\": " << value.order << ",\n";
   os << "      \"elements\": " << value.elements << ",\n";
   os << "      \"ndofs\": " << value.ndofs << ",\n";
   os << "      \"boundary_faces\": " << value.boundary_faces << ",\n";
   os << "      \"radiation_boundary_faces\": "
      << value.radiation_boundary_faces << ",\n";
   os << "      \"assemble_s\": " << value.assemble_s << ",\n";
   os << "      \"solve_s\": " << value.solve_s << ",\n";
   os << "      \"max_iterations\": " << value.max_iterations << ",\n";
   os << "      \"max_relative_residual\": "
      << value.max_relative_residual << ",\n";
   os << "      \"samples\": [\n";
   for (size_t i = 0; i < value.samples.size(); ++i)
   {
      const auto &sample = value.samples[i];
      os << "        {"
         << "\"frequency_hz\":" << sample.frequency_hz << ","
         << "\"pressure_real_pa\":" << sample.pressure_real_pa << ","
         << "\"pressure_imag_pa\":" << sample.pressure_imag_pa << ","
         << "\"iterations\":" << sample.iterations << ","
         << "\"relative_residual\":" << sample.relative_residual << ","
         << "\"converged\":" << (sample.converged ? "true" : "false")
         << "}" << (i + 1 == value.samples.size() ? "" : ",") << "\n";
   }
   os << "      ]\n";
   os << "    }" << (trailing_comma ? "," : "") << "\n";
}

void WriteJson(
   const std::string &path,
   const std::vector<OrderResult> &orders,
   double density_kg_m3,
   double sound_speed_m_s,
   double source_x,
   double source_y,
   double source_z,
   double receiver_x,
   double receiver_y,
   double receiver_z,
   double source_amplitude_m3_s,
   double frequency_start_hz,
   double frequency_stop_hz,
   double frequency_step_hz)
{
   std::ofstream os(path, std::ios::binary);
   if (!os) { throw std::runtime_error("cannot open output file: " + path); }

   os << std::setprecision(17);
   os << "{\n";
   os << "  \"schema_version\": \"r100b-mfem-radiation-transfer-raw-1\",\n";
   os << "  \"mfem_version\": \"" << MFEM_VERSION_STRING << "\",\n";
   os << "  \"compiled_geometry\": \"6x4x2.5-structured-6x4x2-hex\",\n";
   os << "  \"fourier_sign\": \"exp(-i*omega*t)\",\n";
   os << "  \"radiation_normal\": \"outward-from-region-xmax\",\n";
   os << "  \"radiation_boundary_attribute\": "
      << kRadiationBoundaryAttribute << ",\n";
   os << "  \"radiation_boundary_equation\": \"dp_dn_minus_i_k_p_eq_0\",\n";
   os << "  \"complex_operator\": \"K-k^2M-i*k*B_rad\",\n";
   os << "  \"real_block_operator\": \"[A0,+kB;-kB,A0]\",\n";
   os << "  \"source_rhs\": \"[0,-omega*rho*Q*f]\",\n";
   os << "  \"preconditioner\": \"block-diagonal-jacobi-on-K+k^2M+kB\",\n";
   os << "  \"mesh_divisions\": ["
      << kNx << ", " << kNy << ", " << kNz << "],\n";
   os << "  \"density_kg_m3\": " << density_kg_m3 << ",\n";
   os << "  \"sound_speed_m_s\": " << sound_speed_m_s << ",\n";
   os << "  \"source_position_m\": ["
      << source_x << ", " << source_y << ", " << source_z << "],\n";
   os << "  \"receiver_position_m\": ["
      << receiver_x << ", " << receiver_y << ", " << receiver_z << "],\n";
   os << "  \"source_amplitude_m3_s\": " << source_amplitude_m3_s << ",\n";
   os << "  \"frequency_grid_hz\": {"
      << "\"start\":" << frequency_start_hz << ","
      << "\"stop\":" << frequency_stop_hz << ","
      << "\"step\":" << frequency_step_hz << "},\n";
   os << "  \"orders\": [\n";
   for (size_t i = 0; i < orders.size(); ++i)
   {
      WriteOrder(os, orders[i], i + 1 != orders.size());
   }
   os << "  ]\n";
   os << "}\n";
}

} // namespace

int ProbeMain(int argc, char *argv[])
{
   double density_kg_m3 = 1.2;
   double sound_speed_m_s = 343.0;
   double source_x = 1.0, source_y = 2.0, source_z = 1.0;
   double receiver_x = 5.0, receiver_y = 2.0, receiver_z = 1.0;
   double source_amplitude_m3_s = 1.0;
   double frequency_start_hz = 20.0;
   double frequency_stop_hz = 300.0;
   double frequency_step_hz = 1.0;
   int order_min = 2;
   int order_max = 5;
   std::string output;

   for (int i = 1; i < argc; ++i)
   {
      const std::string arg = argv[i];
      auto value = [&](const char *name)
      {
         if (i + 1 >= argc)
         {
            throw std::runtime_error(std::string("missing value for ") + name);
         }
         return std::string(argv[++i]);
      };

      if (arg == "--density") { density_kg_m3 = std::stod(value("--density")); }
      else if (arg == "--sound-speed") { sound_speed_m_s = std::stod(value("--sound-speed")); }
      else if (arg == "--source-x") { source_x = std::stod(value("--source-x")); }
      else if (arg == "--source-y") { source_y = std::stod(value("--source-y")); }
      else if (arg == "--source-z") { source_z = std::stod(value("--source-z")); }
      else if (arg == "--receiver-x") { receiver_x = std::stod(value("--receiver-x")); }
      else if (arg == "--receiver-y") { receiver_y = std::stod(value("--receiver-y")); }
      else if (arg == "--receiver-z") { receiver_z = std::stod(value("--receiver-z")); }
      else if (arg == "--source-amplitude") { source_amplitude_m3_s = std::stod(value("--source-amplitude")); }
      else if (arg == "--frequency-start") { frequency_start_hz = std::stod(value("--frequency-start")); }
      else if (arg == "--frequency-stop") { frequency_stop_hz = std::stod(value("--frequency-stop")); }
      else if (arg == "--frequency-step") { frequency_step_hz = std::stod(value("--frequency-step")); }
      else if (arg == "--order-min") { order_min = std::stoi(value("--order-min")); }
      else if (arg == "--order-max") { order_max = std::stoi(value("--order-max")); }
      else if (arg == "--output") { output = value("--output"); }
      else { throw std::runtime_error("unknown argument: " + arg); }
   }

   if (output.empty()) { throw std::runtime_error("--output is required"); }
   if (!(density_kg_m3 > 0.0 && sound_speed_m_s > 0.0 && source_amplitude_m3_s > 0.0))
   {
      throw std::runtime_error("density, sound speed and source amplitude must be positive");
   }
   if (!(frequency_start_hz > 0.0 && frequency_stop_hz >= frequency_start_hz
         && frequency_step_hz > 0.0))
   {
      throw std::runtime_error("invalid frequency grid");
   }
   if (order_min < 1 || order_max < order_min || order_max > 6)
   {
      throw std::runtime_error("invalid p-refinement range");
   }

   std::vector<OrderResult> orders;
   for (int order = order_min; order <= order_max; ++order)
   {
      orders.push_back(SolveOrder(
         order,
         density_kg_m3,
         sound_speed_m_s,
         source_x,
         source_y,
         source_z,
         receiver_x,
         receiver_y,
         receiver_z,
         source_amplitude_m3_s,
         frequency_start_hz,
         frequency_stop_hz,
         frequency_step_hz));
   }

   WriteJson(
      output,
      orders,
      density_kg_m3,
      sound_speed_m_s,
      source_x,
      source_y,
      source_z,
      receiver_x,
      receiver_y,
      receiver_z,
      source_amplitude_m3_s,
      frequency_start_hz,
      frequency_stop_hz,
      frequency_step_hz);
   return 0;
}

int main(int argc, char *argv[])
{
   try
   {
      return ProbeMain(argc, argv);
   }
   catch (const std::exception &exc)
   {
      std::cerr << "R100B_MFEM_RADIATION_FATAL: " << exc.what() << std::endl;
      return 2;
   }
   catch (...)
   {
      std::cerr << "R100B_MFEM_RADIATION_FATAL: unknown exception" << std::endl;
      return 3;
   }
}
